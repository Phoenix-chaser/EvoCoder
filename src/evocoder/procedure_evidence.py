"""Public receipts for feature checks and green patches, bound to a single solver run."""

import ast
import re
from difflib import SequenceMatcher

from evocoder.contracts import Task, digest


PUBLIC_CASE_FILE = "public_checks/test_reported_layout.py"
STATES = ("enabled", "disabled")


def _public_nodes(task):
    """Recognize only the named, frozen public test functions in the learning bundle."""
    if PUBLIC_CASE_FILE not in task.files or PUBLIC_CASE_FILE in task.editable:
        return {}
    source = task.files[PUBLIC_CASE_FILE]
    definitions = {node.name for node in ast.parse(source).body if isinstance(node, ast.FunctionDef)}
    return {state: f"{PUBLIC_CASE_FILE}::test_feature_{state}" for state in STATES
            if f"test_feature_{state}" in definitions}


def _pytest_states(output, nodes):
    # Pytest verbose progress and the -rA summary. A listed/skipped/xfailing test does
    # not attest that the advertised behavior succeeded. Ignore arbitrary substring hits.
    outcomes = {}
    for state, node in nodes.items():
        escaped = re.escape(node)
        labels = r"(PASSED|FAILED)"
        patterns = (rf"^{escaped} {labels}(?:\s+\[\s*\d+%\])?\s*$",
                    rf"^{labels} {escaped}(?: - .*)?\s*$")
        seen = {m.group(1).lower() for expr in patterns for m in re.finditer(expr, output, re.M)}
        if len(seen) == 1:
            outcomes[state] = seen.pop()
    return outcomes


def step_edits(before, after):
    """Ordered, lossless edits to the actual procedural steps (zero-based slice offsets)."""
    result = []
    for op, a, b, c, d in SequenceMatcher(None, before, after, autojunk=False).get_opcodes():
        if op != "equal":
            result.append({"op": op, "before_index": a, "after_index": c,
                           "before": before[a:b], "after": after[c:d]})
    return result


def behavior_receipt(store, task: Task, run_id: str) -> dict:
    """Separate solver-requested frozen tests, saves/restores, and post-submission checks.

    Other commands remain traceable observations. A model's prose or arbitrary command output
    cannot establish that both feature states were checked.
    """
    run = store.run(run_id)
    task_hash = digest(task.model_dump())
    if run["task_hash"] != task_hash or run["task_id"] != task.id or run["split"] != task.split:
        raise ValueError("procedure receipt task mismatch")
    events = store.events(run_id)
    start, = [e["payload"] for e in events if e["kind"] == "START"]
    if start["task"] != task.public():
        raise ValueError("procedure receipt has unbound public task")
    nodes = _public_nodes(task)
    original = digest({path: task.files[path] for path in task.editable})
    tested, saved, restored, failed_restores, extra, submitted = [], [], [], [], [], []
    green = {}
    image = run["result"].get("image_id")
    checked_sources = set()

    def verify_source(key):
        if key not in checked_sources:
            source = store.get_artifact(key)
            if digest(source) != key or set(source) != set(task.editable) or not all(
                isinstance(text, str) for text in source.values()
            ):
                raise ValueError("checkpoint source does not match editable snapshot")
            checked_sources.add(key)

    for e in events:
        seq, kind, payload = e["seq"], e["kind"], e["payload"]
        if kind in {"CHECKPOINT_TESTED", "PUBLIC_CHECKPOINT", "CHECKPOINT_RESTORED", "CHECKPOINT_RESTORE_FAILED"}:
            key = payload["patch_hash"]
            verify_source(key)
            if (not image or payload["image_id"] != image or payload["task_hash"] != task_hash
                    or payload["argv"] != task.public_command or type(payload["exit_code"]) is not int
                    or type(payload["timeout"]) is not bool
                    or payload["changed_from_input"] is not (key != original)):
                raise ValueError("checkpoint does not bind the command, image, or patch")
            entry = {"seq": seq, "patch_hash": key, "model_step": payload["model_step"],
                     "exit_code": payload["exit_code"], "timeout": payload["timeout"],
                     "changed_from_input": key != original, "output_hash": payload["output_hash"]}
            if kind == "CHECKPOINT_TESTED":
                tested.append(entry)
            elif kind == "PUBLIC_CHECKPOINT":
                if not tested or (key, payload["model_step"]) != (
                    tested[-1]["patch_hash"], tested[-1]["model_step"]
                ) or payload["exit_code"] or payload["timeout"] or tested[-1]["exit_code"] or tested[-1]["timeout"]:
                    raise ValueError("saved checkpoint has no corresponding clean green test")
                if payload["output_hash"] != tested[-1]["output_hash"]:
                    raise ValueError("saved checkpoint clean-test output differs")
                green[key] = seq
                saved.append(entry)
            elif kind == "CHECKPOINT_RESTORE_FAILED":
                if key not in green or not (payload["exit_code"] or payload["timeout"]):
                    raise ValueError("failed restore must recheck an eligible checkpoint")
                failed_restores.append({**entry, "saved_seq": green[key]})
            else:
                previous = payload["previous_patch_hash"]
                if previous is not None:
                    verify_source(previous)
                if key not in green or key != payload["restored_patch_hash"] or payload["exit_code"] or payload["timeout"]:
                    raise ValueError("restore not bound to a prior green checkpoint and successful recheck")
                restored.append({**entry, "saved_seq": green[key], "previous_patch_hash": previous,
                                 "changed_workspace": previous != key if previous is not None else None})
        elif kind in {"OBSERVATION", "PUBLIC_VERIFY"}:
            action, observation = payload["action"], payload["observation"]
            if kind == "PUBLIC_VERIFY":
                if (action.get("argv") != task.public_command or observation.get("image_id") != image
                        or not run["result"].get("patch")):
                    raise ValueError("unbound public submission verification")
                submitted.append({"seq": seq, "patch_hash": run["result"]["patch"],
                                  "exit_code": observation["exit_code"],
                                  "named_states": _pytest_states(observation.get("output", ""), nodes)})
            elif action["tool"] == "restore_checkpoint":
                possible = [entries[-1] for entries in (restored, failed_restores)
                            if entries and "observation_seq" not in entries[-1]]
                if len(possible) != 1:
                    raise ValueError("unbound restore observation")
                observed = possible[0]
                if (observation["exit_code"] != observed["exit_code"]
                        or digest(observation.get("output", "")) != observed["output_hash"]
                        or observation.get("restored_checkpoint") != (observed["patch_hash"] if "changed_workspace" in observed else None)):
                    raise ValueError("restore observation disagrees with checkpoint recheck")
                observed.update(observation_seq=seq, named_states=_pytest_states(observation.get("output", ""), nodes))
            elif action["tool"] == "command" and action["argv"] != task.public_command:
                extra.append({"seq": seq, "argv_hash": digest(action["argv"]), "exit_code": observation["exit_code"]})
            elif (action["tool"] == "test" or action["tool"] == "command" and action["argv"] == task.public_command):
                if tested and "observation_seq" not in tested[-1]:
                    observed = tested[-1]
                    if (observation["exit_code"] != observed["exit_code"]
                            or bool(observation.get("timeout")) != observed["timeout"]
                            or digest(observation.get("output", "")) != observed["output_hash"]):
                        raise ValueError("solver test observation disagrees with clean checkpoint test")
                    observed.update(observation_seq=seq, named_states=_pytest_states(observation.get("output", ""), nodes))

    if any("observation_seq" not in entry for entry in [*tested, *restored, *failed_restores]):
        raise ValueError("checkpoint test has no recorded solver observation")
    return {"schema": 1, "run_id": run_id, "task_hash": task_hash, "split": task.split,
            "image_id": image, "public_command_hash": digest(task.public_command), "public_nodes": nodes,
            "solver_requested_public_tests": tested, "saved_green": saved, "restored_green": restored,
            "failed_restores": failed_restores, "other_solver_commands": extra, "submission_public_tests": submitted}


def proposal_view(receipt):
    """Bound the projection and distinguish archive proofs from procedural advice."""
    tests = receipt["solver_requested_public_tests"]
    selected = tests[:1] + tests[-3:] if len(tests) > 4 else tests
    return {"registered_public_feature_states": sorted(receipt["public_nodes"]),
            "solver_requested_frozen_tests_total": len(tests),
            "solver_requested_frozen_tests": [
                {k: row[k] for k in ("seq", "named_states", "exit_code", "changed_from_input")} for row in selected
            ],
            "saved_green": [{"seq": r["seq"], "changed_from_input": r["changed_from_input"]}
                            for r in receipt["saved_green"]],
            "restored_green": [{"seq": r["seq"], "saved_seq": r["saved_seq"], "changed_workspace": r["changed_workspace"]}
                              for r in receipt["restored_green"]],
            "failed_restore_sequences": [r["seq"] for r in receipt["failed_restores"]],
            "other_solver_command_sequences": [r["seq"] for r in receipt["other_solver_commands"]],
            "submitted_patch_public_checks": [{"seq": r["seq"], "named_states": r["named_states"],
                                              "exit_code": r["exit_code"]} for r in receipt["submission_public_tests"]]}
