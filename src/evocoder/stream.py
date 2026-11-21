from __future__ import annotations

import json
from pathlib import Path

from evocoder.contracts import Settings, canonical, digest
from evocoder.dataset import load_catalog
from evocoder.evolution import Evolution
from evocoder.runtime import Runner
from evocoder.store import Store


def learn_stream(settings: Settings, store: Store, manifest: Path, output: Path,
                 max_proposals: int = 3, with_final_test=False, initial_candidate: str | None = None,
                 feedback_policy="final_public_failure") -> dict:
    """A bounded feedback loop. Failed gates become evidence only through later learn executions."""
    if not 1 <= max_proposals <= 3:
        raise ValueError("core protocol allows 1..3 proposals per stream")
    if output.exists():
        raise ValueError("stream output must be new; previous experiments are immutable")
    tasks = load_catalog(manifest)
    learn = [t for t in tasks if t.split == "learn"]
    dev = [t for t in tasks if t.split == "dev"]
    if len(learn) < 2 or len(dev) < 2:
        raise ValueError("stream needs at least two learning and two development tasks")
    output.mkdir(parents=True)
    runner = Runner(settings, store)
    evolution = Evolution(settings, store, feedback_policy=feedback_policy, grounding_tasks={t.id: t for t in learn})
    starting_active = store.active()
    if initial_candidate:
        with store.connect() as db:
            if not db.execute("SELECT 1 FROM skills WHERE id=?", (initial_candidate,)).fetchone():
                raise ValueError("initial candidate is not an existing skill")
    pending = initial_candidate
    run_ids, timeline, proposal_ids = [], [], []
    for task in learn:
        # Candidate trial is explicitly confined to learning tasks; published tasks use active.
        selected = pending or store.active()
        run_id = runner.solve(task, selected, use_active=False, candidate=bool(pending))
        run_ids.append(run_id)
        run = store.run(run_id)
        item = {"task_id": task.id, "run_id": run_id, "selected_skill": selected,
                "applied_skill": run["skill_id"],
                "candidate_trial": bool(pending), "public_failed": run["result"]["public_failed"],
                "state": run["state"], "active_before": store.active()}
        if run["state"] in {"ERROR", "INTERRUPTED"}:
            item["decision"] = "stop_on_infrastructure_or_model_error"
            timeline.append(item)
            break
        feedback = evolution.revision_feedback(run, store.events(run_id))
        should_propose = selected is None or (run["skill_id"] == selected and evolution.qualifies({
            "public_failed": run["result"]["public_failed"], "execution_feedback": feedback}))
        item["execution_feedback"] = feedback
        if should_propose and len(proposal_ids) < max_proposals:
            # The bounded frozen window includes the currently observed failure.
            evidence_runs = run_ids[-12:]
            candidate = evolution.propose(evidence_runs, selected)
            proposal_ids.append(candidate)
            support_ids = {store.run(r)["task_id"] for r in evidence_runs}
            report = evolution.evaluate(candidate, dev, [t for t in learn if t.id in support_ids])
            item.update({"candidate": candidate, "receipt": report["receipt"],
                         "gate_passed": report["gate"]["passed"]})
            if report["gate"]["passed"]:
                store.activate(candidate, report["receipt"], expected=item["active_before"])
                pending = None
                item["decision"] = "activate_verified_revision"
            else:
                pending = candidate
                item["decision"] = "keep_active_try_candidate_on_next_learning_task"
        else:
            item["decision"] = "skip_nonapplicable_skill" if selected and run["skill_id"] is None else "keep_current_version"
        item["active_after"] = store.active()
        timeline.append(item)
        # Persist after every task so progress is reviewable even if a later stage fails.
        (output / "timeline.json").write_text(json.dumps(timeline, ensure_ascii=False, indent=2) + "\n")
    final_ids = []
    completed = not any(t["decision"] == "stop_on_infrastructure_or_model_error" for t in timeline)
    frozen_skill = store.active()
    before = len(evolution.lineage())
    if with_final_test and completed:
        for task in tasks:
            if task.split == "test":
                final_ids.append(runner.solve(task, frozen_skill, use_active=False))
    if len(evolution.lineage()) != before or store.active() != frozen_skill:
        raise RuntimeError("final test violated frozen-state contract")
    report = {"schema": 1, "mode": settings.mode,
              "claim": "engineering_validation_only" if settings.mode == "mock" else "exploratory",
              "manifest_hash": digest(json.loads(manifest.read_text())),
              "config_hash": digest(settings.public()), "starting_active": starting_active,
              "active": store.active(), "initial_candidate": initial_candidate, "feedback_policy": feedback_policy,
              "proposal_count": len(proposal_ids), "timeline": timeline,
              "final_runs": [store.run(r) for r in final_ids], "lineage": evolution.lineage(),
              "budget": store.budget(), "final_test_learning": False}
    report["completed"] = completed
    (output / "summary.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    all_ids = [*run_ids, *final_ids]
    for item in timeline:
        if item.get("receipt"):
            for row in store.get_artifact(item["receipt"])["rows"]:
                all_ids.extend([row["baseline_run"], row["candidate_run"]])
    traces = output / "traces"
    traces.mkdir()
    for run_id in all_ids:
        (traces / f"{run_id}.json").write_text(canonical({"run": store.run(run_id),
                                                        "events": store.events(run_id)}) + "\n")
    return report
