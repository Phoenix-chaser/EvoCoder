"""Separately preregister recovered-failure learning after the strict-policy pilot.

No hidden grading output or dev/test outcomes enter the proposal. Reused final probes
are labelled as such, and the same cumulative state/budget is retained.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from evocoder.contracts import Settings, digest
from evocoder.dataset import load_catalog
from evocoder.evolution import Evolution
from evocoder.runtime import Runner
from evocoder.store import Store

ROOT = Path("experiments/live-003")
DATA = Path("experiments/live-002/dataset/manifest.json")
SEED = "8e1999dda10186753f8eff01768cd9145bcab2fdab01fea35308f02f31478f80"


def dump(path, body):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(body, ensure_ascii=False, indent=2) + "\n")


def fingerprint():
    return digest({str(p): p.read_text() for p in [*sorted(Path("src/evocoder").glob("*.py")),
                                                 Path("scripts/run_feedback_revision.py")]})


def prepare():
    if (ROOT / "protocol.json").exists():
        raise ValueError("protocol already frozen")
    settings = Settings()
    store = Store(settings.state_dir)
    previous = json.loads(Path("experiments/live-002/summary.json").read_text())
    runs = [r["run_id"] for r in previous["timeline"]]
    evo = Evolution(settings, store, feedback_policy="post_edit_or_budget")
    evidence_hash, evidence = evo.freeze(runs, SEED)
    protocol = {"schema": 1, "model": settings.model, "state": str(settings.state_dir),
                "budget_before": store.budget(), "source_hash": fingerprint(),
                "manifest_hash": digest(json.loads(DATA.read_text())), "initial_active": store.active(),
                "feedback_policy": "post_edit_or_budget", "evidence_hash": evidence_hash,
                "evidence_runs": runs, "seed": SEED, "max_proposals": 1,
                "max_steps": settings.max_steps,
                "selection": "one revision from prior public learn feedback; no manual edit or regeneration",
                "dev_gate": "unchanged: two group wins, zero regressions or infrastructure errors",
                "additional_activation_gate": "candidate must not regress against no-skill dev control",
                "probe_arms": ["no_skill", "frozen_seed", "revision"],
                "probe_reuse": "same two test tasks already executed in live-002; not untouched final test",
                "hypothesis_changed_after_pilot": True, "hidden_feedback_to_proposer": False,
                "evidence_feedback": [{"run_id": e["run_id"], "public_failed": e["public_failed"],
                                       "execution_feedback": e["execution_feedback"]}
                                      for e in evidence["episodes"]]}
    dump(ROOT / "protocol.json", protocol)
    print(json.dumps(protocol, ensure_ascii=False, indent=2))


def execute():
    settings = Settings()
    settings.validate_live()
    if settings.mode != "live":
        raise ValueError("live configuration required")
    protocol = json.loads((ROOT / "protocol.json").read_text())
    if fingerprint() != protocol["source_hash"] or digest(json.loads(DATA.read_text())) != protocol["manifest_hash"]:
        raise ValueError("frozen code or dataset changed")
    if (ROOT / "result.json").exists():
        raise ValueError("experiment already completed")
    store = Store(settings.state_dir)
    if store.active() != protocol["initial_active"]:
        raise ValueError("active version changed")
    with store.connect() as db:
        before_ids = {r[0] for r in db.execute("SELECT id FROM runs")}
        before_calls = {r[0] for r in db.execute("SELECT id FROM calls")}
    evo = Evolution(settings, store, feedback_policy="post_edit_or_budget")
    candidate = evo.propose(protocol["evidence_runs"], SEED)
    dump(ROOT / "candidate.json", {"skill_id": candidate, "skill": store.get_artifact(candidate)})
    tasks = load_catalog(DATA)
    dev, learn = [t for t in tasks if t.split == "dev"], [t for t in tasks if t.split == "learn"]
    evaluation = evo.evaluate(candidate, dev, learn)
    dump(ROOT / "evaluation.json", evaluation)
    runner = Runner(settings, store)
    control = []
    for task in dev:
        run_id = runner.solve(task, None, use_active=False, purpose="evaluate")
        run = store.run(run_id)
        control.append({"task_id": task.id, "run_id": run_id, "state": run["state"],
                        "passed": run["result"]["passed"], "cost": run["result"]["cost"]})
    candidate_by_task = {r["task_id"]: r for r in evaluation["rows"] if r["split"] == "dev"}
    vs_control_regressions = [r["task_id"] for r in control if r["passed"] and
                              not candidate_by_task[r["task_id"]]["candidate_pass"]]
    control_infra = [r["task_id"] for r in control if r["state"] == "ERROR"]
    activate = evaluation["gate"]["passed"] and not vs_control_regressions and not control_infra
    if activate:
        store.activate(candidate, evaluation["receipt"], expected=protocol["initial_active"])
    dump(ROOT / "activation.json", {"activated": activate, "active": store.active(),
                                    "no_skill_regressions": vs_control_regressions,
                                    "no_skill_infrastructure_errors": control_infra, "control": control})
    frozen_active, frozen_lineage = store.active(), len(evo.lineage())
    matrix = []
    for task in tasks:
        if task.split != "test":
            continue
        for arm, skill_id in [("no_skill", None), ("frozen_seed", SEED), ("revision", candidate)]:
            run_id = runner.solve(task, skill_id, use_active=False, purpose="evaluate")
            run = store.run(run_id)
            matrix.append({"task_id": task.id, "arm": arm, "run_id": run_id, "skill_id": skill_id,
                           "state": run["state"], "passed": run["result"]["passed"],
                           "steps": run["result"]["steps"], "cost": run["result"]["cost"]})
    if store.active() != frozen_active or len(evo.lineage()) != frozen_lineage:
        raise ValueError("probes changed frozen strategy state")
    with store.connect() as db:
        ids = [r[0] for r in db.execute("SELECT id FROM runs ORDER BY created") if r[0] not in before_ids]
        new_calls = [r[0] for r in db.execute("SELECT id FROM calls") if r[0] not in before_calls]
    result = {"schema": 1, "mode": "live", "claim": "exploratory_feedback_policy_revision",
              "protocol": protocol, "skill_id": candidate,
              "lineage": next(e for e in evo.lineage() if e["id"] == candidate),
              "evaluation": evaluation, "no_skill_dev_control": control,
              "no_skill_regressions": vs_control_regressions, "activated": activate,
              "active": store.active(), "reused_probe_matrix": matrix,
              "budget_before": protocol["budget_before"], "budget_after": store.budget(),
              "new_call_count": len(new_calls), "new_runs": [store.run(i) for i in ids],
              "probe_learning": False}
    dump(ROOT / "result.json", result)
    for run_id in ids:
        dump(ROOT / "traces" / f"{run_id}.json", {"run": store.run(run_id), "events": store.events(run_id)})
    print(json.dumps({"skill_id": candidate, "gate": evaluation["gate"], "activated": activate,
                      "active": store.active(), "budget": store.budget(), "new_calls": len(new_calls)},
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--root", type=Path, default=ROOT)
    args = parser.parse_args()
    ROOT = args.root
    execute() if args.run else prepare()
