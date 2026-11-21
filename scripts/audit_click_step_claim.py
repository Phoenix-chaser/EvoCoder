"""Audit one explicitly specified historical assertion in the frozen GLM skill step."""
import argparse
import json
import re
from pathlib import Path

from evocoder.contracts import digest
from evocoder.dataset import load_catalog
from evocoder.procedure_evidence import PUBLIC_CASE_FILE, behavior_receipt
from replay_click_checkpoint import Archive, read


CANDIDATE = "4a88b651b305e5e5fa1563d7e62453c232de18e61605462411a8be3c933dfdf9"
STEP_INDEX = 6
QUOTE = "(observed: hidden-state 'done=8' persisted while the enabled state passed after the first edit)"
TASK = "pallets-click-f316d5cb3a"
TASK_HASH = "ea0752d9da2f05720099b076574635cbeccf5a3c06c780edf57814537251316c"
STATES = {"enabled": "passed", "disabled": "failed"}
HEAD = re.compile(r"^_+ (test_feature_\w+) _+\s*$", re.M)


def disabled_actual_line(output):
    """Extract the actual side of the frozen public disabled node's failing comparison."""
    headings = list(HEAD.finditer(output))
    for index, heading in enumerate(headings):
        if heading.group(1) == "test_feature_disabled":
            end = headings[index + 1].start() if index + 1 < len(headings) else len(output)
            for line in output[heading.end():end].splitlines():
                if line.startswith(("===", "___")):
                    break
                if re.fullmatch(r"E\s+\+ done=8\s*", line):
                    return line.strip()
    return None


def classify(checks):
    # A single occurrence must satisfy the entire assertion. First-public-check is a
    # relaxation: more than one successful edit can precede the first test.
    matches = [c for c in checks if c["task_id"] == TASK and c["named_states"] == STATES and c["disabled_actual_line"]]
    exact = [c for c in matches if c["first_changed_public_check"] and len(c["observed_edit_or_write_seqs"]) == 1]
    if exact:
        return "supported", matches, exact
    if not matches:
        return "unsupported_no_matching_invocation", matches, []
    return "unsupported_timing", matches, []


def audit(root):
    revision = read(root / "revision.json")
    protocol = read(root / "protocol.json")
    archive = Archive(root / "seed" / "evocoder.sqlite3")
    child = archive.get_artifact(CANDIDATE)
    step = child["steps"][STEP_INDEX]
    if (revision["candidate_id"] != CANDIDATE or revision["candidate"] != child or digest(child) != CANDIDATE
            or step.count(QUOTE) != 1 or protocol["parent"] != child["parent"]):
        raise ValueError("case-specific audit requires the frozen skill, exact quoted assertion and parent")
    with archive.connect() as db:
        reg = db.execute("SELECT parent,evidence_hash FROM skills WHERE id=?", (CANDIDATE,)).fetchone()
    refs = child["evidence_refs"]
    if not reg or tuple(reg) != (child["parent"], revision["frozen_evidence"]):
        raise ValueError("candidate or evidence registration changed")
    frozen = archive.get_artifact(reg["evidence_hash"])
    episodes = {e["run_id"]: e for e in frozen["episodes"]}
    if (len(episodes) != len(frozen["episodes"]) or frozen["parent_id"] != child["parent"]
            or len(set(refs)) != len(refs) or not set(refs) <= set(episodes)):
        raise ValueError("citation outside frozen proposal evidence")
    manifest = Path(protocol["learn_manifest"])
    if digest(read(manifest)) != protocol["learn_hash"]:
        raise ValueError("learning manifest changed")
    tasks = {t.id: t for t in load_catalog(manifest)}
    if not all(t.split == "learn" for t in tasks.values()) or digest(tasks[TASK].model_dump()) != TASK_HASH:
        raise ValueError("expected the registered progress-bar learning snapshot")
    cases = []
    evidence = []
    for ref in refs:
        run = archive.run(ref)
        task = tasks[run["task_id"]]
        if (run["skill_id"] != child["parent"] or run["split"] != "learn" or run["purpose"] != "solve"
                or run["mode"] != "live" or run["config"]["model"] != "glm-5.3"):
            raise ValueError("historical assertion must cite a GLM parent learning application")
        events = archive.events(ref)
        by_seq = {e["seq"]: e for e in events}
        receipt = behavior_receipt(archive, task, ref)
        if receipt != read(root / "behavior" / (ref + ".json")):
            raise ValueError("stored procedural evidence differs from the learning archive")
        frozen_events = episodes[ref]["events"]
        if any(by_seq[e["seq"]] != e for e in frozen_events):
            raise ValueError("public learning events differ from proposal evidence")
        frozen_seqs = {e["seq"] for e in frozen_events}
        edits = [e["seq"] for e in events if e["kind"] == "OBSERVATION"
                 and e["payload"]["action"]["tool"] in {"edit", "write"} and e["payload"]["observation"]["exit_code"] == 0]
        evidence.append({"run_id": ref, "task_id": task.id, "task_hash": receipt["task_hash"],
                         "behavior_hash": digest(receipt), "task_matches_claim": task.id == TASK})
        changed = [row for row in receipt["solver_requested_public_tests"] if row["changed_from_input"]]
        for index, row in enumerate(changed):
            seq = row["observation_seq"]
            if seq not in frozen_seqs:
                raise ValueError("solver observation missing from the frozen learning evidence")
            output = by_seq[seq]["payload"]["observation"]["output"]
            cases.append({"run_id": ref, "task_id": task.id, "task_hash": receipt["task_hash"],
                          "checkpoint_tested_seq": row["seq"], "observation_seq": seq, "output_hash": row["output_hash"],
                          "patch_hash": row["patch_hash"], "named_states": row["named_states"],
                          "first_changed_public_check": index == 0, "observed_edit_or_write_seqs": [s for s in edits if s < row["seq"]],
                          "disabled_actual_line": disabled_actual_line(output) if task.id == TASK else None})
    verdict, joint, exact = classify(cases)
    return {"schema": 1, "method": "One manually specified historical assertion checked against cited learning executions.",
            "candidate": CANDIDATE, "parent": child["parent"], "step_index_zero_based": STEP_INDEX,
            "step": step, "historical_assertion": QUOTE, "evidence_refs": refs, "frozen_evidence": reg["evidence_hash"],
            "case_mapping": {"task_id": TASK, "task_hash": TASK_HASH, "frozen_public_test_file": PUBLIC_CASE_FILE,
                             "frozen_public_test_hash": digest(tasks[TASK].files[PUBLIC_CASE_FILE]),
                             "enabled": "test_feature_enabled calls exercise(False), displaying progress",
                             "disabled": "test_feature_disabled calls exercise(True), hiding progress"},
            "required_joint_observation": {"same_run_same_changed_patch_same_public_invocation": True,
                                           "named_states": STATES, "disabled_actual_text": "done=8",
                                           "timing": "first recorded changed public test following one successful edit/write"},
            "cited_runs": evidence, "checks": cases, "same_invocation_matches": joint, "exact_matches": exact,
            "verdict": verdict, "accepted_as_historical_fact": verdict == "supported",
            "scope": "Only the quoted factual parenthetical was tested. Missing results between edits are unobserved. "
                     "Procedural recommendations and other skill text have not been audited by this predicate.",
            "model_calls": 0, "gate_effect": "none"}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    opts = parser.parse_args()
    report = audit(opts.root)
    opts.output.parent.mkdir(parents=True, exist_ok=True)
    with opts.output.open("x") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)
        f.write("\n")
    print(report["verdict"], len(report["same_invocation_matches"]), len(report["checks"]))
