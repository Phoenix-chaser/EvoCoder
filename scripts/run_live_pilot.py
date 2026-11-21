"""Frozen, exploratory local pilot. Reuses self-built tasks, not a SWE-bench result.

Preparation and trusted reference validation are offline. Only --run sends paid requests.
Use the same settings/state budget as the connection checks.
"""
from __future__ import annotations

import argparse
import json
import runpy
from pathlib import Path

from evocoder.contracts import Settings, Task, canonical, digest
from evocoder.runtime import grade
from evocoder.store import Store
from evocoder.stream import learn_stream

ROOT = Path("experiments/live-001")
SOURCE = Path("../CodeAgent-R/src/agentlab/benchmark/hard_tasks.py")
ASSIGNMENT = {
    "hard_cache_invalidation_001": "learn",
    "hard_resource_cleanup_001": "learn",
    "hard_api_contract_001": "learn",
    "hard_state_machine_001": "learn",
    "hard_dependency_tracker_001": "dev",
    "hard_config_merge_001": "dev",
    "hard_pipeline_race_001": "test",
    "hard_thread_counter_001": "test",
}


def prepare(source=SOURCE):
    destination = ROOT / "pilot-dataset"
    destination.mkdir(parents=True, exist_ok=True)
    specs = runpy.run_path(str(source))["HARD_SPECS"]
    specs = {s["task_id"]: s for s in specs}
    entries, reference = [], []
    settings = Settings().model_copy(update={"image": "evocoder-tests:20261115"})
    for task_id, split in ASSIGNMENT.items():
        spec = specs[task_id]
        task = Task(id=task_id, source_group=spec["source_group"], split=split,
                    repository="local-codeagent-r/" + spec["source_group"],
                    issue=spec["instruction"],
                    files={spec["module"]: spec["buggy"], **spec.get("extra_files", {}),
                           "test_public.py": spec["public_test"]},
                    editable=[spec["module"]],
                    public_command=["python", "-m", "pytest", "-q", "test_public.py"],
                    grader_files=spec["private_tests"],
                    grader_command=["python", "-m", "pytest", "-q", "test_public.py",
                                    *sorted(spec["private_tests"])])
        path = destination / (task_id + ".json")
        content = canonical(task.model_dump()) + "\n"
        if path.exists() and path.read_text() != content:
            raise ValueError("frozen pilot task changed")
        path.write_text(content)
        entries.append({"path": path.name, "hash": digest(task.model_dump()), "id": task_id,
                        "split": split, "source_group": task.source_group})
        # Reference code is used only here by the trusted controller, never placed in solver files.
        result = grade(task, {spec["module"]: spec["fixed"]}, settings, "reference-" + task_id)
        reference.append({"task_id": task_id, **result})
        print(json.dumps({"reference_task": task_id, "passed": result["passed"]}), flush=True)
        if not result["passed"]:
            raise RuntimeError("reference validation failed; do not start paid pilot")
    manifest = {"schema": 1, "kind": "reused_local_selfbuilt_exploratory_tasks",
                "source": source.name, "source_sha256": __import__("hashlib").sha256(source.read_bytes()).hexdigest(),
                "known_to_developer": True, "tasks": entries}
    path = destination / "manifest.json"
    if path.exists() and json.loads(path.read_text()) != manifest:
        raise ValueError("frozen pilot manifest changed")
    path.write_text(canonical(manifest) + "\n")
    (ROOT / "reference-validation.json").write_text(json.dumps(reference, indent=2) + "\n")
    protocol = {"schema": 1, "purpose": "live contract and exploratory skill-revision pilot",
                "not_claimed": ["natural GitHub issues", "SWE-bench", "untouched independent final test",
                                "statistical evidence", "four-arm comparison"],
                "manifest_hash": digest(manifest), "model": settings.model,
                "budget_cap_usd": settings.budget_usd, "max_proposals": 2,
                "max_steps": settings.max_steps, "image": settings.image,
                "gate": "at least 2 dev source-group wins, no observed regression or infrastructure errors",
                "assignment": ASSIGNMENT, "frozen_before_paid_pilot": True,
                "preflight_costs_share_same_state": True,
                "test_policy": "freeze active strategy; no test-to-learning writeback"}
    (ROOT / "protocol.json").write_text(json.dumps(protocol, ensure_ascii=False, indent=2) + "\n")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, default=SOURCE, help="path to the CodeAgent-R hard task module")
    parser.add_argument("--run", action="store_true", help="send budgeted paid model calls")
    args = parser.parse_args()
    if not args.run:
        prepare(args.source)
        return
    if not (ROOT / "protocol.json").exists():
        raise ValueError("prepare/freeze/validate the pilot before paid execution")
    settings = Settings().model_copy(update={"image": "evocoder-tests:20261115"})
    if settings.mode != "live":
        raise ValueError("this runner requires explicitly configured live mode")
    settings.validate_live()
    report = learn_stream(settings, Store(settings.state_dir), ROOT / "pilot-dataset/manifest.json",
                          ROOT / "pilot-report", max_proposals=2, with_final_test=True)
    print(json.dumps({"completed": report["completed"], "active": report["active"],
                      "proposals": report["proposal_count"], "timeline": report["timeline"],
                      "final": [{"task": r["task_id"], "state": r["state"], "skill": r["skill_id"]}
                                for r in report["final_runs"]], "budget": report["budget"]},
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
