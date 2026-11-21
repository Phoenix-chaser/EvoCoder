"""Package and replay one actual GLM-authored learning check on the pinned Click sources."""
import argparse
import difflib
import json
from pathlib import Path

from verify_revision_example import digest

DEFAULT = Path(__file__).resolve().parents[1] / "examples/click-cli-check.json"


def edits(original, now):
    hunks = {}
    for path, new in now.items():
        if original[path] == new:
            continue
        before, after = original[path].splitlines(True), new.splitlines(True)
        hunks[path] = [{"at": a, "old": before[a:b], "new": after[c:d]} for op, a, b, c, d in
                       difflib.SequenceMatcher(None, before, after, autojunk=False).get_opcodes() if op != "equal"]
    return hunks


def apply(original, hunks):
    result = dict(original)
    for path, replacements in hunks.items():
        lines = result[path].splitlines(True)
        for hunk in reversed(replacements):
            pos, old = hunk["at"], hunk["old"]
            if lines[pos:pos + len(old)] != old:
                raise ValueError("archived edit differs from pinned source")
            lines[pos:pos + len(old)] = hunk["new"]
        result[path] = "".join(lines)
    return result


def create(root, report, out):
    from evocoder.revision_diagnostic import read
    from report_click_procedure import Archive
    from verify_authored_cli_checks import entries
    result = read(root / "result.json")
    evidence = read(report / "authored-checks.json")
    reach = {(v["run"], v["seq"]): v for v in read(report / "cli-reach.json")["checks"]}
    trials = {t["run_id"]: t for t in evidence["trials"] if t["phase"] in ("learning", "learning_replay")}
    candidates = [v for v in evidence["checks"] if v["run"] in trials]
    if not candidates:
        return False
    # This is a teaching example. Cohort-wide denominators are reported separately.
    example = max(candidates, key=lambda c: (reach.get((c["run"], c["seq"]), {}).get("caught_disabled", False),
                  trials[c["run"]]["phase"] == "learning_replay", c["tool"] == "cli_check", -len(c["source"])))
    guided = None
    coverage_retry = None
    if (report / "check-repair-result.json").is_file():
        guided_report = read(report / "check-repair-result.json")
        # Prefer the child to illustrate the same learning patch before and after a directed retry.
        chosen = max(guided_report["trials"], key=lambda t: (t["role"] == "child", any(a["caught_disabled"] for a in t["attempts"])))
        example, = [c for c in candidates if (c["run"], c["seq"]) == (chosen["run"], chosen["seq"])]
        guided = next((a for a in chosen["attempts"] if a.get("caught_disabled")), chosen["attempts"][-1])
        if (report / "coverage-feedback-result.json").is_file():
            coverage_retry, = [a for a in read(report / "coverage-feedback-result.json")["rows"]
                               if a["run"] == chosen["run"] and a["patch"] == chosen["patch"]]
    trial, p = trials[example["run"]], result["protocol"]
    archive = Archive(root / "seed/evocoder.sqlite3")
    from verify_authored_cli_checks import ENV
    events = archive.events(example["run"])
    action, = [a["payload"] for a, b in zip(events, events[1:]) if b["seq"] == example["seq"] and a["kind"] == "ACTION"]
    argv = [*ENV, example["source"]] if action["tool"] == "cli_check" else action["argv"]
    mid = trial["task_id"]
    manifest = Path(p["learn_manifest"])
    task, ref = entries(manifest, [mid])[mid]
    ctrl = read(manifest.parent / "controller" / (mid + ".json"))
    patch = archive.get_artifact(example["patch"])
    original = {k: task.files[k] for k in task.editable}
    hunks = edits(original, patch)
    if apply(original, hunks) != patch:
        raise ValueError("incorrect patch extraction")
    item = {"study_hash": digest(p), "model": p["settings"]["model"], "image": p["settings"]["image"],
            "task": mid, "task_hash": digest(task.model_dump()), "upstream": ctrl["upstream"],
            "base_commit": ctrl["base_commit"], "fix_commit": ctrl["fixed_commit"],
            "original_hash": digest(original), "reference_hash": digest(ref), "edits": hunks,
            "phase": trial["phase"], "skill": trial["skill"], "parent": p["parent"], "child": result["candidate"],
            "check": example, "argv": argv, "reach": reach.get((example["run"], example["seq"])),
            "overall_repair_passed": trial["passed"], "directed_check_repair": guided,
            "coverage_feedback_retry": coverage_retry}
    out.write_text(json.dumps({"payload_hash": digest(item), "payload": item}, ensure_ascii=False, indent=2) + "\n")
    return True


def verify(sample):
    item = sample["payload"]
    c = item["check"]
    key = item["parent"] if item["phase"] == "learning" else item["child"]
    if (digest(item) != sample["payload_hash"] or item["phase"] not in ("learning", "learning_replay")
            or item["skill"] != key or digest(c["source"]) != c["source_hash"]
            or any(c[k]["image"] != item["image"] for k in ("original_action", "reference", "off_mutant"))):
        raise ValueError("portable study example failed integrity validation")
    if c["verdict"] == "detects_disabled_regression" and not (
            c["original_action"]["exit"] == c["live_exit"] and c["reference"]["exit"] == 0
            and c["off_mutant"]["termination"] == "authored_assertion" and c["off_mutant"]["line"]
            and c["original_action"]["calls"] and any(a != b for a, b in zip(c["reference"]["calls"], c["off_mutant"]["calls"]))):
        raise ValueError("stored execution does not support the verdict")
    reached = item.get("reach")
    if reached and reached["caught_disabled"]:
        if (not reached["raw_execution_matches"] or not reached["matched_call_indices"]
                or c["verdict"] != "detects_disabled_regression"):
            raise ValueError("no matching off-state invocation on the action-time patch")
        for i in reached["matched_call_indices"]:
            orig, good, bad = (reached[n][i] for n in ("original", "reference", "off_mutant"))
            if (not orig["input"]["stack"] or orig["input"] != good["input"] or good["input"] != bad["input"]
                    or (good["exit"], good["output_hash"]) == (bad["exit"], bad["output_hash"])):
                raise ValueError("off-state call identity or output differs from receipt")
    for retry in (item.get("directed_check_repair"), item.get("coverage_feedback_retry")):
        if retry and (digest(retry["source"]) != retry["source_hash"] or (retry["caught_disabled"] and not (
                retry["raw_execution_matches"] and retry["matched_call_indices"]
                and retry["reference"]["exit"] == 0 and retry["off_mutant"]["termination"] == "authored_assertion"))):
            raise ValueError("directed retry does not support its recorded verdict")
    return item


def reproduce(item, repo, image=None):
    from evocoder.contracts import Settings
    from evocoder.sandbox import bounded_process
    from prepare_click_missing_state import CASES, off_control
    from prepare_click_procedure import task_case
    from verify_authored_cli_checks import replay, verdict
    [spec] = [s for s in CASES if s["commit"] == item["fix_commit"]]
    task, metadata = task_case(repo, spec)
    original = {k: task.files[k] for k in task.editable}
    c, ref = item["check"], metadata["reference"]
    patch = apply(original, item["edits"])
    if (digest(task.model_dump()) != item["task_hash"] or digest(original) != item["original_hash"]
            or digest(patch) != c["patch"] or digest(ref) != item["reference_hash"]
            or metadata["base_commit"] != item["base_commit"]):
        raise ValueError("upstream, task or action-time source differs")
    image_id = item["image"]
    if image:
        info = bounded_process(["docker", "image", "inspect", image, "--format", "{{.Id}}"], 10)
        if info["timeout"] or info["exit_code"] or not info["output"].strip().startswith("sha256:"):
            raise ValueError("local replay image unavailable")
        image_id = info["output"].strip()
    opts = Settings(_env_file=None, image=image_id)
    observations = []
    for name, source in (("original_action", patch), ("reference", ref), ("off_mutant", off_control(task, ref))):
        fresh = replay(task, source, c["source"], opts)
        if fresh != {**c[name], "image": image_id}:
            raise ValueError(f"{name} replay differs from archived execution")
        observations.append(fresh)
    if verdict(*observations, c["live_exit"], image_id) != c["verdict"]:
        raise ValueError("replayed verdict differs")
    if item.get("reach"):
        from evocoder.runtime import validation
        from verify_cli_reach import observed as observe_calls
        live = validation({**task.files, **patch}, item["argv"], opts, "portable-raw-replay")
        if (item["reach"]["raw_execution_matches"] and
                (live["timeout"] or live["exit_code"] != c["live_exit"] or digest(live["output"][:10000]) != c["live_output_hash"])):
            raise ValueError("actual solver command no longer reproduces its original exit/output")
        for name, code in (("original", patch), ("reference", ref), ("off_mutant", off_control(task, ref))):
            if observe_calls(task, code, c["source"], opts) != item["reach"][name]:
                raise ValueError("invocation-level reach replay differs")
    for retry in (item.get("directed_check_repair"), item.get("coverage_feedback_retry")):
        if not retry or not retry["source"]:
            continue
        from evocoder.runtime import validation
        from verify_authored_cli_checks import ENV
        from verify_cli_reach import observed as observe_calls
        raw = validation({**task.files, **patch}, [*ENV, retry["source"]], opts, "portable-directed")
        if raw["output"][:10000] != retry["output"] or raw["exit_code"] != retry["exit"]:
            raise ValueError("directed retry no longer matches its original execution")
        for name, calls, code in (("original_action", "original_calls", patch), ("reference", "reference_calls", ref),
                                  ("off_mutant", "mutant_calls", off_control(task, ref))):
            if (replay(task, code, retry["source"], opts) != {**retry[name], "image": image_id}
                    or observe_calls(task, code, retry["source"], opts) != retry[calls]):
                raise ValueError("directed retry replay differs")
    return image_id


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--sample", type=Path, default=DEFAULT)
    ap.add_argument("--click-git", type=Path, help="locally replay the three Docker executions using this Click mirror")
    ap.add_argument("--image", help="optionally compare behavior on a different locally available Docker image")
    ap.add_argument("--export", type=Path, help="finished study root for producing the sample")
    ap.add_argument("--report", type=Path, help="audited report folder, used by --export")
    args = ap.parse_args()
    if args.export and (not args.report or not create(args.export, args.report, args.sample)):
        ap.error("export requires a verified study report with at least one authored learning script")
    result = verify(json.loads(args.sample.read_text()))
    if args.image and not args.click_git:
        ap.error("--image requires --click-git")
    image_id = reproduce(result, args.click_git, args.image) if args.click_git else None
    c = result["check"]
    print(json.dumps({"model": result["model"], "task": result["task"], "phase": result["phase"],
                      "caught_disabled": bool((result.get("reach") or {}).get("caught_disabled")),
                      "directed_retry_caught_disabled": (result.get("directed_check_repair") or {}).get("caught_disabled"),
                      "coverage_feedback_caught_disabled": (result.get("coverage_feedback_retry") or {}).get("caught_disabled"),
                      "repair_passed": result["overall_repair_passed"],
                      "live_exit": c["live_exit"], "upstream_exit": c["reference"]["exit"],
                      "off_mutant_exit": c["off_mutant"]["exit"], "assertion_line": c["off_mutant"]["line"]
                      if c["off_mutant"].get("line") else None,
                      "replay": "stored_receipt" if image_id is None else
                      "exact_image" if image_id == result["image"] else "compatible_image",
                      **({"replay_image": image_id} if image_id else {})}, ensure_ascii=False))
