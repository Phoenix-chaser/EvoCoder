"""Offline, post-hoc probes for submitted patches on the two Click route-study development cases."""

import argparse
import difflib
import json
import sqlite3
from collections import Counter
from contextlib import closing
from pathlib import Path
from textwrap import dedent

from evocoder.contracts import Settings, Task, digest, safe_path
from evocoder.runtime import validation


COUNT = "pallets-click-3d029e03eb"
HINT = "pallets-click-42e2821ba3"
HEADER = "EVO_CLICK_CASES="
EXPLICIT_COUNT = "count_with_explicit_bounds"
PROBES = {
    COUNT: """
        import click
        import json
        from click.testing import CliRunner

        def example(flag, attrs):
            @click.command(name="inspect")
            @click.option(flag, help="Describe this option.", **attrs)
            def inspect(**kwargs):
                pass

            result = CliRunner().invoke(inspect, ["--help"], terminal_width=120)
            # Inspect the actual option row, including leading, trailing and internal spaces.
            prefix = "  " + flag
            lines = [x for x in result.output.splitlines(True) if x.startswith(prefix)]
            return {"exit": result.exit_code, "rows": lines}

        cases = {
            "implicit_count": example("-v", {"count": True}),
            "count_with_default": example("-v", {"count": True, "default": 2, "show_default": True}),
            "explicit_numeric_bounds": example("--level", {"type": click.IntRange(1, 5)}),
            "default_and_bounds": example("--level", {
                "type": click.IntRange(1, 5), "default": 3, "show_default": True}),
        }
        # The public issue asks about explicit bounds on other numeric options.
        # Check the upstream behavior of an explicitly bounded count separately.
        try:
            cases["count_with_explicit_bounds"] = example("-v", {"count": True, "type": click.IntRange(1, 5)})
        except Exception as exc:
            cases["count_with_explicit_bounds"] = {"exception": type(exc).__name__}
        print("EVO_CLICK_CASES=" + json.dumps(cases))
    """,
    HINT: """
        import click
        import json
        from click.testing import CliRunner

        def example(enabled, help_names, nested=False):
            @click.command(name="inspect", add_help_option=enabled,
                           context_settings={"help_option_names": help_names})
            @click.argument("item")
            def inspect(item):
                pass

            command, args = inspect, []
            if nested:
                @click.group(name="team")
                def team():
                    pass
                team.add_command(inspect)
                command, args = team, ["inspect"]
            result = CliRunner().invoke(command, args, terminal_width=120)
            # Full output retains the order, number of blank lines and terminal newline.
            return {"exit": result.exit_code, "output": result.output}

        cases = {
            "default_help": example(True, ["--help"]),
            "disabled_help": example(False, ["--help"]),
            "custom_help": example(True, ["-h"]),
            "nested_custom_help": example(True, ["-h"], nested=True),
        }
        print("EVO_CLICK_CASES=" + json.dumps(cases))
    """,
}


def expect(value, message):
    if not value:
        raise ValueError(message)


def read(path):
    return json.loads(path.read_text())


def extract_probe(result):
    lines = [line[len(HEADER):] for line in result["output"].splitlines() if line.startswith(HEADER)]
    if result["exit_code"] or result["timeout"] or len(lines) != 1:
        return {"error": "probe_failed", "exit": result["exit_code"], "timeout": result["timeout"]}
    return {"cases": json.loads(lines[0])}


def validate_reference(task_id, cases):
    if task_id == COUNT:
        names = {"implicit_count", "count_with_default", "explicit_numeric_bounds", "default_and_bounds",
                 EXPLICIT_COUNT}
        expect(set(cases) == names, "count reference cases missing")
        for item in cases.values():
            expect(item.get("exit") == 0 and len(item.get("rows", [])) == 1
                   and "Describe this option." in item["rows"][0],
                   "upstream reference did not render the requested option")
        def row(name):
            return cases[name]["rows"][0]

        expect("[" not in row("implicit_count") and "default:" in row("count_with_default")
               and "1" in row("explicit_numeric_bounds") and "5" in row("explicit_numeric_bounds")
               and "default:" in row("default_and_bounds") and "5" in row("default_and_bounds"),
               "upstream numeric annotations missing")
        expect("[1<=x<=5]" in row(EXPLICIT_COUNT), "upstream count option must show both explicit inclusive bounds")
    else:
        expected = {"default_help": "inspect --help", "disabled_help": None,
                    "custom_help": "inspect -h", "nested_custom_help": "team inspect -h"}
        expect(set(cases) == set(expected), "help reference cases missing")
        for name, hint in expected.items():
            output = cases[name]["output"]
            expect(cases[name]["exit"] == 2 and output.startswith("Usage: ")
                   and "\n\nError:" in output and "\n\n\n" not in output, "reference usage-error layout invalid")
            if hint:
                expect(f'Try "{hint}" for help.' in output, "upstream help option/path differs")
            else:
                expect('Try "' not in output, "upstream did not omit disabled help")


def task_and_reference(manifest, entries, task_id):
    entry, = [e for e in entries if e["id"] == task_id]
    task = Task.model_validate(read(manifest.parent / safe_path(entry["path"])))
    control = read(manifest.parent / "controller" / (task_id + ".json"))
    expect(task.split == "dev" and task.id == task_id and digest(task.model_dump()) == entry["hash"]
           == control["task_hash"] and set(control["reference"]) == set(task.editable), "invalid development snapshot")
    return task, control["reference"]


def replay(task, patch, settings, *, regrade):
    files = {**task.files, **patch}
    label = "click-route-probe-" + task.id[-10:] + "-" + digest(patch)[:12]
    probe = validation(files, ["env", "PYTHONPATH=/workspace/src:/workspace", "python", "-c",
                               dedent(PROBES[task.id])], settings, label)
    expect(probe["image_id"] == settings.image, "probe image differs from frozen image")
    result = extract_probe(probe)
    if regrade:
        # Hidden output stays in this grader; only status leaves the fresh sandbox.
        judge = validation({**files, **task.grader_files}, task.grader_command, settings, label + "-grade")
        expect(judge["image_id"] == settings.image, "independent grading image differs")
        result["independent_replay"] = {"exit": judge["exit_code"], "timeout": judge["timeout"]}
    return result


def patch_diff(task, patch):
    return "".join(line for path in task.editable for line in difflib.unified_diff(
        task.files[path].splitlines(True), patch[path].splitlines(True), fromfile="a/"+path, tofile="b/"+path))


def load_trial(db, run_id, row, tasks):
    # The copied state also contains previous studies. Never select other tasks/splits.
    raw = db.execute("SELECT task_id,task_hash,state,skill_id,result FROM runs "
                     "WHERE id=? AND split='dev' AND purpose='evaluate' AND task_id IN (?,?)",
                     (run_id, COUNT, HINT)).fetchone()
    if raw is None:
        expect(row is None, "paired focus run missing")
        return None
    task_id, task_hash, state, skill_id, body = raw
    task = tasks[task_id]
    outcome = json.loads(body)
    expect(task_hash == digest(task.model_dump()), "trace task hash differs from frozen task")
    events = [(seq, kind, json.loads(payload)) for seq, kind, payload in db.execute(
        "SELECT seq,kind,payload FROM events WHERE run_id=? ORDER BY seq", (run_id,))]
    if row:
        arm = row[1]
        expect(row[0]["task_hash"] == task_hash and row[0][arm+"_applied_skill"] == skill_id
               and row[0][arm+"_pass"] == outcome["passed"], "pair differs from trace")
    start, = [e for _, k, e in events if k == "START"]
    decisions = [e for _, k, e in events if k == "SKILL_ROUTE"]
    commands = []
    for seq, kind, ev in events:
        if kind not in {"PUBLIC_VERIFY","OBSERVATION"}:
            continue
        action, obs = ev["action"], ev["observation"]
        if action["tool"] in {"test","command"}:
            argv = task.public_command if action["tool"] == "test" else action["argv"]
            commands.append({"seq": seq, "kind": kind, "argv": argv, "exit": obs["exit_code"],
                             "timeout": obs.get("timeout", False)})
    patch = None
    public_exit = final_grade = None
    key = outcome.get("patch")
    if key:
        patch = json.loads(db.execute("SELECT body FROM artifacts WHERE id=?", (key,)).fetchone()[0])
        diff, = [v for _, k, v in events if k == "PATCH"]
        check, = [v for _, k, v in events if k == "PUBLIC_VERIFY"]
        final, = [v for _, k, v in events if k == "GRADE"]
        expect(diff["artifact"] == key == digest(patch) and set(patch) == set(task.editable)
               and diff["diff"] == patch_diff(task, patch),
               "submitted patch failed integrity check")
        expect(final["passed"] == outcome["passed"] and
               final["grader_hash"] == digest({"files":task.grader_files,"command":task.grader_command}),
               "grade differs from task or trace")
        public_exit, final_grade = check["observation"]["exit_code"], final["passed"]
    return task_id, patch, {
        "run_id": run_id, "task_id": task_id, "state": state, "paired": row is not None,
        "repeat": row[0]["repeat"] + 1 if row else None, "policy": row[0]["policy"] if row else None,
        "arm": row[1] if row else None, "requested_skills": start.get("requested_skill_ids"),
        "actual_route": decisions[-1]["decision"] if decisions else None, "applied_skill": skill_id,
        "model_steps": outcome.get("model_steps"), "exhaustion": outcome.get("exhaustion_reason"),
        "patch_hash": key, "passed": outcome.get("passed"), "submitted_public_exit": public_exit,
        "recorded_final_grade": final_grade, "commands": commands,
    }


def compare_expected(observed, reference):
    if "cases" not in observed:
        return {}, "probe_failed"
    expect(observed["cases"].keys() == reference.keys(), "incomplete case output")
    differences = {k: {"expected": expected, "observed": observed["cases"][k]}
                   for k, expected in reference.items() if observed["cases"][k] != expected}
    return differences, "mismatch" if differences else "matches_reference"


def scope_differences(task_id, observed, reference):
    differences, status = compare_expected(observed, reference)
    compatibility = {}
    if task_id == COUNT and EXPLICIT_COUNT in differences:
        compatibility[EXPLICIT_COUNT] = differences.pop(EXPLICIT_COUNT)
    if status != "probe_failed":
        status = "mismatch" if differences else "matches_reference"
    return {"status": status, "differences": differences, "compatibility_differences": compatibility}


def evaluate(root):
    root = root.resolve()
    protocol, result = read(root/"protocol.json"), read(root/"result.json")
    expect(protocol["claim"] in {"routing_intervention_diagnostic", "independent_learn_procedure_revision", "missing_cli_state_revision"}
           and result["protocol"] == protocol,
           "post-hoc analysis requires terminal route-study records")
    if protocol["claim"] in {"independent_learn_procedure_revision", "missing_cli_state_revision"}:
        expected = {(task_id, repeat) for task_id in PROBES for repeat in (0, 1)}
        expect(result["completed"] and result["candidate"] and len(result["rows"]) == len(expected)
               and {(r["task_id"], r["repeat"]) for r in result["rows"] if r["policy"] == "normal"} == expected,
               "procedure study focus scoring must be closed before probes")
    manifest = Path(protocol["manifest"])
    catalog = read(manifest)
    expect(protocol["manifest_hash"] == digest(catalog), "changed manifest")
    targets = {k: task_and_reference(manifest, catalog["tasks"], k) for k in PROBES}
    tasks = {k: v[0] for k, v in targets.items()}
    opts = Settings(_env_file=None, mode="mock", image=protocol["settings"]["image"],
                    command_timeout=protocol["settings"]["command_timeout"])
    rows = {r[a + "_run"]: (r,a) for r in result["rows"] if r["task_id"] in tasks for a in ("baseline","candidate")}
    trial_ids = list(dict.fromkeys([*rows, *result["run_ids"]]))
    dbpath = root/"seed/evocoder.sqlite3"
    wal = dbpath.with_name(dbpath.name + "-wal")
    query = "?mode=ro" if wal.exists() and wal.stat().st_size else "?mode=ro&immutable=1"
    trials, patches, hashes = [], {}, {}
    with closing(sqlite3.connect(dbpath.as_uri() + query, uri=True)) as db:
        for run_id in trial_ids:
            loaded = load_trial(db, run_id, rows.get(run_id), tasks)
            if loaded:
                tid, patch, trial = loaded
                trials.append(trial)
                if patch is not None:
                    patches[tid, trial["patch_hash"]] = patch
    cases, observations = {}, {}
    for task_id, (task, ref) in targets.items():
        ref_key = digest(ref)
        original_key = digest({k:task.files[k] for k in task.editable})
        all_patches = {key: v for (tid, key), v in patches.items() if tid == task_id}
        all_patches[ref_key] = ref
        all_patches[original_key] = {k:task.files[k] for k in task.editable}
        hashes[task_id] = {"reference": ref_key, "original": original_key, "task": digest(task.model_dump())}
        results = {}
        for key, value in all_patches.items():
            results[key] = replay(task, value, opts, regrade=(task_id,key) in patches)
            results[key]["changed_files"] = [path for path in task.editable if value[path] != task.files[path]]
            results[key]["unified_diff"] = patch_diff(task, value)
        reference = results[ref_key]["cases"]
        validate_reference(task.id, reference)
        expect(scope_differences(task_id, results[original_key], reference)["status"] == "mismatch",
               "probe does not distinguish original and fixed versions")
        cases[task_id] = reference
        for obs in results.values():
            obs.update(scope_differences(task_id, obs, reference))
        observations[task_id] = results
    counts = Counter()
    for t in trials:
        key = t["patch_hash"]
        counts["submitted" if key else "no_submission"] += 1
        if not key:
            continue
        obs = observations[t["task_id"]][key]
        t["probe_status"], t["mismatched_cases"] = obs["status"], sorted(obs["differences"])
        t["compatibility_mismatched_cases"] = sorted(obs["compatibility_differences"])
        regrade = obs["independent_replay"]
        t["replay_verdict_matches_record"] = (not regrade["timeout"] and
            ((t["submitted_public_exit"] == 0 and regrade["exit"] == 0) == t["recorded_final_grade"]))
        counts[t["probe_status"]] += 1
        counts["replayed_verdict_disagreement"] += int(not t["replay_verdict_matches_record"])
        if obs["status"] == "mismatch" and t["recorded_final_grade"]:
            counts["original_grade_pass_with_public_probe_mismatch"] += 1
        if obs["status"] == "matches_reference" and not t["recorded_final_grade"]:
            counts["original_grade_fail_beyond_selected_public_cases"] += 1
    summary = [{k:t[k] for k in ("run_id","task_id","repeat","policy","arm","actual_route","patch_hash",
                                  "submitted_public_exit","recorded_final_grade","mismatched_cases")}
               for t in trials if t.get("mismatched_cases")]
    compatibility = [{k:t[k] for k in ("run_id","task_id","repeat","policy","arm","actual_route","patch_hash",
                                        "recorded_final_grade","compatibility_mismatched_cases")}
                     for t in trials if t.get("compatibility_mismatched_cases")]
    scopes = {tid: {key: "upstream_compatibility" if tid == COUNT and key == EXPLICIT_COUNT else "public_issue"
                    for key in reference} for tid,reference in cases.items()}
    return {"schema":1, "study":str(root), "result_hash":digest(result), "model_calls":0,
            "phase":result["phase"], "completed":result["completed"], "runtime_image":opts.image,
            "purpose":"Post-hoc diagnostics; public issue and upstream compatibility cases are reported separately.",
            "probe_scope":scopes, "compatibility_counterexamples":compatibility,
            "task_hashes":hashes, "probe_code":{k:dedent(v) for k,v in PROBES.items()},
            "unique_submitted_patches":len(patches), "run_counts":dict(counts), "counterexamples":summary,
            "reference_cases":cases, "observations":observations, "trials":trials}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("choose a new output file")
    report = evaluate(args.root)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)
        f.write("\n")
    print(json.dumps({"runs":report["run_counts"],"unique":report["unique_submitted_patches"],
                      "mismatch_count":len(report["counterexamples"])}, ensure_ascii=False))
