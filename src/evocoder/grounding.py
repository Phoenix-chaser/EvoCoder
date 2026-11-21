"""Bind skill-revision summaries to immutable submissions and observed learning outcomes."""

import ast
import difflib
import re
from collections import defaultdict
from collections.abc import Mapping

from evocoder.contracts import Task, digest


RECEIPT_MARKER = "use_evidence_receipts"


def _definitions(source):
    """Preserve repeated definitions (e.g. overloads and property setters)."""
    tree = ast.parse(source)
    definitions = defaultdict(list)

    def collect(node, scope):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                name = ".".join((*scope, child.name))
                if not isinstance(child, ast.ClassDef):
                    definitions[name].append(ast.dump(child, include_attributes=False))
                collect(child, (*scope, child.name))
            else:
                collect(child, scope)

    collect(tree, ())
    return definitions


def _files(task, patch):
    if set(patch) != set(task.editable) or not all(isinstance(s, str) for s in patch.values()):
        raise ValueError("submitted patch does not cover the editable snapshot")
    changed = []
    for path in sorted(task.editable):
        before, after = task.files[path], patch[path]
        if before == after:
            continue
        symbols = None
        if path.endswith(".py"):
            try:
                old, new = _definitions(before), _definitions(after)
                symbols = sorted(k for k in old.keys() | new.keys() if old.get(k) != new.get(k))
            except (SyntaxError, ValueError, RecursionError):
                # A syntax error still leaves a verifiable file change.
                pass
        changed.append({"path": path, "before_hash": digest(before),
                        "after_hash": digest(after), "symbols": symbols})
    return changed


def _test_status(observation):
    if type(observation.get("exit_code")) is not int:
        raise ValueError("test event has no exit code")
    if observation.get("timeout") is True:
        return "timed_out"
    return "passed" if observation["exit_code"] == 0 else "failed"


def _receipt(store, episode, task, run):
    if (run["split"], run["purpose"]) != ("learn", "solve") or task.split != "learn":
        raise ValueError("summary receipts require learning solver runs")
    if run["state"] not in {"COMPLETE", "FAILED", "EXHAUSTED"} or run["task_hash"] != digest(task.model_dump()):
        raise ValueError("learning run or frozen task snapshot mismatch")
    events = store.events(run["id"])
    public_kinds = {"START", "OBSERVATION", "PATCH", "PUBLIC_VERIFY", "SKILL_ROUTE", "ACTION_REJECTED"}
    frozen_events = [e for e in events if e["kind"] in public_kinds]
    if frozen_events != episode["events"] or run["skill_id"] != episode["skill_id"]:
        raise ValueError("learning events disagree with frozen evidence")
    starts = [e for e in events if e["kind"] == "START"]
    if len(starts) != 1 or starts[0]["payload"]["task"] != task.public():
        raise ValueError("unbound task in learning trace")
    result = run["result"]
    if result["public_failed"] is not episode["public_failed"]:
        raise ValueError("frozen public status disagrees with run result")
    patch_hash = result["patch"]
    finals = [e for e in events if e["kind"] in {"PATCH", "PUBLIC_VERIFY", "GRADE"}]
    receipt = {
        "run_id": run["id"], "task_id": task.id, "task_hash": run["task_hash"], "state": run["state"],
        "patch_hash": patch_hash, "patch_seq": None, "files": [],
        "public_check": {"status": "not_run", "seq": None, "scope": "submitted"},
        "final_grade": {"status": "not_run", "seq": None},
    }
    if patch_hash is None:
        if finals or run["state"] != "EXHAUSTED":
            raise ValueError("unsubmitted patch conflicts with terminal evidence")
        # The last in-workspace test may precede later edits. Never label it as
        # verification of a submitted patch.
        for event in reversed(events):
            if event["kind"] != "OBSERVATION":
                continue
            action = event["payload"]["action"]
            if action["tool"] == "test" or (
                action["tool"] == "command" and action.get("argv") == task.public_command
            ):
                receipt["public_check"] = {"status": _test_status(event["payload"]["observation"]),
                                           "seq": event["seq"], "scope": "in_workspace"}
                break
        return receipt
    if run["state"] not in {"COMPLETE", "FAILED"} or [e["kind"] for e in finals] != ["PATCH", "PUBLIC_VERIFY", "GRADE"]:
        raise ValueError("submission lacks an ordered patch, public verification and final grade")
    patch_event, public_event, grade_event = finals
    patch = store.get_artifact(patch_hash)
    if patch_hash != patch_event["payload"]["artifact"] or digest(patch) != patch_hash:
        raise ValueError("submitted patch integrity mismatch")
    receipt["files"] = _files(task, patch)
    diff = "".join(
        line for path in task.editable
        for line in difflib.unified_diff(task.files[path].splitlines(True), patch[path].splitlines(True),
                                         fromfile="a/" + path, tofile="b/" + path)
    )
    if diff != patch_event["payload"]["diff"]:
        raise ValueError("patch event diff disagrees with submitted source")
    pub = public_event["payload"]
    if pub["action"].get("tool") != "test" or pub["action"].get("argv") != task.public_command:
        raise ValueError("public verification command differs from task")
    observation = pub["observation"]
    grade = grade_event["payload"]
    if (type(grade.get("passed")) is not bool or grade["passed"] is not result["passed"]
            or (run["state"] == "COMPLETE") != grade["passed"]
            or grade.get("grader_hash") != digest({"files": task.grader_files, "command": task.grader_command})
            or not result.get("image_id") or grade.get("image_id") != result["image_id"]
            or observation.get("image_id") != result["image_id"]):
        raise ValueError("final grade disagrees with the frozen task or run result")
    status = _test_status(observation)
    if result["public_failed"] is not (observation["exit_code"] != 0) or (status != "passed" and grade["passed"]):
        raise ValueError("inconsistent submitted public verification")
    receipt["patch_seq"] = patch_event["seq"]
    receipt["public_check"] = {"status": status, "seq": public_event["seq"], "scope": "submitted"}
    # GRADE is the runner's final verdict (includes the public check).
    # Raw grader output, hidden assertions and the grader hash are never projected.
    final_status = "passed" if grade["passed"] else "failed"
    if grade.get("timeout") is True:
        if grade["passed"]:
            raise ValueError("a timed out grade cannot pass")
        final_status = "timed_out"
    receipt["final_grade"] = {"status": final_status, "seq": grade_event["seq"]}
    return receipt


def build_grounding(store, evidence: dict, tasks: Mapping[str, Task]) -> dict:
    """Build controller-owned receipts from full, hash-matched task snapshots."""
    receipts = {}
    for episode in evidence["episodes"]:
        key = episode["run_id"]
        run = store.run(key)
        if key in receipts or run["id"] != key or run["task_id"] not in tasks:
            raise ValueError("duplicate, missing or unbound learning task")
        receipts[key] = _receipt(store, episode, tasks[run["task_id"]], run)
    if not receipts:
        raise ValueError("empty learning evidence")
    return {"schema": 1, "receipts": receipts}


def _label(value):
    # Filenames and source identifiers are untrusted data, including controls.
    return value if re.fullmatch(r"[A-Za-z0-9_./-]{1,120}", value) else "sha256-" + digest(value)[:16]


def render_summary(catalog: dict, refs: list[str]) -> str:
    """Render only attested observations; truncate detail by omitting whole names."""
    if (not refs or len(refs) > 12 or len(refs) != len(set(refs))
            or any(key not in catalog["receipts"] for key in refs)):
        raise ValueError("summary must cite 1..12 unique evidence receipts")
    lines = []
    for key in refs:
        r = catalog["receipts"][key]
        files = r["files"]
        if r["patch_hash"]:
            patch = f"submitted patch (event {r['patch_seq']}) changed {len(files)} file(s)"
        else:
            patch = "no submitted patch"
        pub, grade = r["public_check"], r["final_grade"]
        label = "last workspace public test" if pub["scope"] == "in_workspace" else "submitted public test"
        public = f"{label} {pub['status']}" + (f" (event {pub['seq']})" if pub["seq"] else "")
        verdict = f"final grade {grade['status']}" + (f" (event {grade['seq']})" if grade["seq"] else "")
        line = f"Run {_label(key)}: {patch}; {public}; {verdict}."
        if "procedure" in r:
            observation = r["procedure"]
            states = sorted({state for row in observation["solver_requested_public_tests"] for state in row["named_states"]})
            line += (f" Frozen tests reported feature states: {', '.join(states) or 'none recorded'}. "
                     f"Green saves: {len(observation['saved_green'])}; verified restores: {len(observation['restored_green'])}.")
        # Keep total well below Skill.change_summary's bound for standard six-run
        # live projections. Full path/symbol lists remain in the persisted receipt.
        details = []
        for entry in files:
            detail = _label(entry["path"])
            symbols = entry["symbols"]
            if symbols:
                detail += " (" + ", ".join(_label(s) for s in symbols[:4]) + (", …" if len(symbols) > 4 else "") + ")"
            if len(line) + sum(len(item) + 2 for item in details) + len(detail) + 10 > 1750 // len(refs):
                break
            details.append(detail)
        if details:
            line += " Touched " + ", ".join(details) + "."
        lines.append(line)
    summary = "\n".join(lines)
    if len(summary) > 2000:
        raise ValueError("rendered grounding exceeds the skill-summary limit")
    return summary


def validate_summary(summary: str, refs: list[str], catalog: dict) -> None:
    if summary != render_summary(catalog, refs):
        raise ValueError("ungrounded revision summary: does not match verified patch and test receipts")
