"""Learning-only draft branches; published portfolios remain evaluator controlled."""

from __future__ import annotations

import json
from pathlib import Path

from evocoder.contracts import Settings, canonical, digest
from evocoder.dataset import load_catalog
from evocoder.evolution import Evolution
from evocoder.errors import ProposalRejected
from evocoder.library import SkillLibrary
from evocoder.runtime import Runner
from evocoder.store import Store


def learning_checkpoint(store: Store, manifest: Path, previous: Path, mode: str) -> dict:
    """Carry forward a verified pre-test prefix stopped by HTTP 402 or HTTP 500.

    The rejected call stays reserved and the failed task is never retried. This is
    deliberately narrower than general crash recovery: no partial proposals/gates.
    """
    report = json.loads((previous / "summary.json").read_text())
    timeline = json.loads((previous / "timeline.json").read_text())
    learn = [
        t for t in load_catalog(manifest)
        if t.split == "learn" and t.id != report.get("skip_seed_task")
    ]
    if (
        report["completed"]
        or report["mode"] != mode
        or report["manifest_hash"] != digest(json.loads(manifest.read_text()))
        or report["proposal_attempts"] != 0
        or report["proposal_count"] != 0
        or report["final_runs"]
        or report["pool"] != report["initial_pool"]
        or report["active_library"] != store.active_library()
        or report["timeline"] != timeline
        or not 1 <= len(timeline) <= len(learn)
        or [t["task_id"] for t in timeline] != [t.id for t in learn[: len(timeline)]]
    ):
        raise ValueError("unsupported or changed learning checkpoint")
    for i, item in enumerate(timeline):
        run = store.run(item["run_id"])
        if (
            run["task_id"] != learn[i].id
            or run["task_hash"] != digest(learn[i].model_dump())
            or run["split"] != "learn"
            or run["purpose"] != "solve"
            or run["mode"] != mode
            or run["state"] != item["state"]
            or item["pool_before"] != report["initial_pool"]
            or item.get("candidate")
        ):
            raise ValueError("learning checkpoint differs from actual executions")
        if i == len(timeline) - 1:
            if run["state"] != "ERROR" or run["result"]["error"] not in {
                "RuntimeError: model_http_402: reservation retained",
                "RuntimeError: model_http_500: reservation retained",
            }:
                raise ValueError("continuation requires a recorded HTTP 402 or HTTP 500 failure")
        elif run["state"] not in {"COMPLETE", "FAILED", "EXHAUSTED"}:
            raise ValueError("earlier checkpoint execution is incomplete")
    return report


def library_stream(
    settings: Settings,
    store: Store,
    manifest: Path,
    output: Path,
    *,
    max_proposals=3,
    with_final_test=False,
    initial_skills: list[str] | None = None,
    feedback_policy="post_edit_or_budget",
    model=None,
    allow_revisions=True,
    allow_new_families=True,
    evaluation_workers=1,
    tolerate_rejected_proposals=False,
    continuation_from: Path | None = None,
    skip_seed_task: str | None = None,
) -> dict:
    if not 1 <= max_proposals <= 3:
        raise ValueError("core protocol allows 1..3 proposals per stream")
    if output.exists():
        raise ValueError("stream output must be new")
    tasks = load_catalog(manifest)
    learn, dev = ([t for t in tasks if t.split == split] for split in ("learn", "dev"))
    if len(learn) < 2 or len({t.source_group for t in dev}) < 2:
        raise ValueError("library stream needs two learn tasks and two dev groups")
    runner = Runner(settings, store, model)
    library = SkillLibrary(settings, store, runner)
    evolution = Evolution(settings, store, model, feedback_policy, grounding_tasks={t.id: t for t in learn})
    starting = store.active_library()
    pool = library.members(starting)
    for key in initial_skills or []:
        pool = library.replace(pool, key)
    # Preflight evidence availability before spending or creating output.
    snapshots = {t.id: digest(t.model_dump()) for t in learn}
    if skip_seed_task is not None and skip_seed_task not in snapshots:
        raise ValueError("the predeclared seed must be a learning task")
    for key in pool:
        if any(snapshots.get(k) != v for k, v in library.provenance(key)["support"].items()):
            raise ValueError("manifest omits or changes initial library evidence")
    checkpoint = None
    if continuation_from:
        checkpoint = learning_checkpoint(store, manifest, continuation_from, settings.mode)
        if (
            pool != checkpoint["pool"]
            or with_final_test
            or checkpoint.get("skip_seed_task") != skip_seed_task
            or any(
                checkpoint[key] != value
                for key, value in {
                    "max_proposals": max_proposals,
                    "feedback_policy": feedback_policy,
                    "allow_revisions": allow_revisions,
                    "allow_new_families": allow_new_families,
                    "evaluation_workers": evaluation_workers,
                    "tolerate_rejected_proposals": tolerate_rejected_proposals,
                }.items()
            )
        ):
            raise ValueError("learning continuation changed the declared policy")
    output.mkdir(parents=True)
    source = {p.name: p.read_text() for p in sorted(Path(__file__).parent.glob("*.py"))}
    source_dir = output / "source"
    source_dir.mkdir()
    for name, body in source.items():
        (source_dir / name).write_text(body)
    protocol = {
        "schema": 1,
        "mode": settings.mode,
        "starting_library": starting,
        "initial_pool": pool,
        "manifest_hash": digest(json.loads(manifest.read_text())),
        "source_hash": digest(source),
        "config_hash": digest(settings.public()),
        "max_proposals": max_proposals,
        "feedback_policy": feedback_policy,
        "final_test_learning": False,
        "skip_seed_task": skip_seed_task,
    }
    protocol.update({"allow_revisions": allow_revisions, "allow_new_families": allow_new_families})
    protocol["evaluation_workers"] = evaluation_workers
    protocol["tolerate_rejected_proposals"] = tolerate_rejected_proposals
    if checkpoint:
        protocol["continuation"] = {
            "from": str(continuation_from.resolve()),
            "checkpoint_hash": digest(checkpoint),
            "retained_tasks": len(checkpoint["timeline"]),
            "failed_task_policy": "retain ERROR, do not retry, continue with next task",
        }
    (output / "protocol.json").write_text(canonical(protocol) + "\n")
    with store.connect() as db:
        before_runs = {r[0] for r in db.execute("SELECT id FROM runs")}
        before_events = {r[0] for r in db.execute("SELECT DISTINCT run_id FROM events")}
    timeline = json.loads(canonical(checkpoint["timeline"])) if checkpoint else []
    proposals, finals = [], []
    proposal_attempts = 0
    error = None
    try:
        feedback_tasks = [task for task in learn if task.id != skip_seed_task]
        for task in feedback_tasks[len(timeline) :]:
            run_id = runner.solve(task, use_active=False, candidate=True, skill_ids=pool)
            run = store.run(run_id)
            applied = run["skill_id"]
            item = {
                "task_id": task.id,
                "run_id": run_id,
                "pool_before": list(pool),
                "applied_skill": applied,
                "state": run["state"],
                "library_before": store.active_library(),
                "decision": "keep_family",
            }
            timeline.append(item)
            if run["state"] in {"ERROR", "INTERRUPTED"}:
                raise RuntimeError("learning execution failed; no retry or further proposals")
            feedback = evolution.revision_feedback(run, store.events(run_id))
            item["execution_feedback"] = feedback
            route = run["result"]["routing"]["decision"]
            revision = applied and evolution.qualifies(
                {"public_failed": run["result"]["public_failed"], "execution_feedback": feedback}
            )
            new_family = not applied and route in {"none", "skip"}
            if revision and not allow_revisions:
                item["decision"] = "revision_disabled_control"
            elif new_family and not allow_new_families:
                item["decision"] = "new_family_disabled_control"
            elif proposal_attempts >= max_proposals:
                item["decision"] = "proposal_budget_exhausted"
            elif new_family and len(pool) == 8:
                item["decision"] = "capacity_reached_no_eviction"
            elif revision or new_family:
                if revision:
                    with store.connect() as db:
                        evidence_key = db.execute(
                            "SELECT evidence_hash FROM skills WHERE id=?", (applied,)
                        ).fetchone()[0]
                    previous = [e["run_id"] for e in store.get_artifact(evidence_key)["episodes"]]
                    evidence_runs = [r for r in previous if r != run_id][-11:] + [run_id]
                else:
                    # An unrelated skipped task starts a root; never attach its failure to another family.
                    evidence_runs = [run_id]
                proposal_attempts += 1
                try:
                    key = evolution.propose(evidence_runs, applied)
                except ProposalRejected as exc:
                    if not tolerate_rejected_proposals:
                        raise
                    item.update(
                        {
                            "decision": "reject_invalid_proposal_keep_current",
                            "proposal_error": str(exc),
                            "library_after": store.active_library(),
                        }
                    )
                    (output / "timeline.json").write_text(canonical(timeline) + "\n")
                    continue
                proposals.append(key)
                pool = library.replace(pool, key)
                item.update({"candidate": key, "parent": applied, "pool_after": list(pool)})
                observed = set()
                for member in [*library.members(store.active_library()), key]:
                    observed.update(library.provenance(member)["support"])
                report = library.evaluate(
                    key, dev, [t for t in learn if t.id in observed], workers=evaluation_workers
                )
                item.update({"receipt": report["receipt"], "gate_passed": report["gate"]["passed"]})
                if report["gate"]["passed"]:
                    store.activate_library(
                        report["library_id"], report["receipt"], item["library_before"], mode=settings.mode
                    )
                    item["decision"] = "publish_verified_library"
                else:
                    item["decision"] = "retain_published_library_keep_learning_draft"
            elif not applied:
                item["decision"] = "uncertain_route_no_new_family"
            item["library_after"] = store.active_library()
            (output / "timeline.json").write_text(canonical(timeline) + "\n")
        frozen = store.active_library()
        with store.connect() as db:
            frozen_skills = [tuple(r) for r in db.execute("SELECT * FROM skills ORDER BY id")]
            frozen_releases = db.execute("SELECT COUNT(*) FROM library_releases").fetchone()[0]
        if with_final_test:
            for task in tasks:
                if task.split == "test":
                    kwargs = {"library_id": frozen} if frozen else {"skill_ids": []}
                    finals.append(runner.solve(task, use_active=False, purpose="evaluate", **kwargs))
                    if store.run(finals[-1])["state"] in {"ERROR", "INTERRUPTED"}:
                        raise RuntimeError("final evaluation error; stopping without retry")
        with store.connect() as db:
            if (
                store.active_library() != frozen
                or frozen_skills != [tuple(r) for r in db.execute("SELECT * FROM skills ORDER BY id")]
                or frozen_releases != db.execute("SELECT COUNT(*) FROM library_releases").fetchone()[0]
            ):
                raise RuntimeError("final evaluation mutated frozen skill library")
    except (KeyboardInterrupt, SystemExit):
        error = "interrupted; no automatic continuation"
        raise
    except Exception as exc:
        error = f"{type(exc).__name__}: {str(exc)[:1200]}"
        if settings.api_key:
            error = error.replace(settings.api_key, "[redacted]")
        if timeline:
            timeline[-1]["error"] = error
    finally:
        report = {
            **protocol,
            "claim": "engineering_validation_only" if settings.mode == "mock" else "exploratory",
            "completed": error is None,
            "error": error,
            "active_library": store.active_library(),
            "pool": pool,
            "proposal_count": len(proposals),
            "proposal_attempts": proposal_attempts,
            "timeline": timeline,
            "final_runs": [store.run(r) for r in finals],
            "budget": store.budget(),
        }
        (output / "timeline.json").write_text(canonical(timeline) + "\n")
        (output / "summary.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        traces = output / "traces"
        traces.mkdir()
        with store.connect() as db:
            run_ids = [r[0] for r in db.execute("SELECT id FROM runs") if r[0] not in before_runs]
        for key in run_ids:
            (traces / f"{key}.json").write_text(
                canonical({"run": store.run(key), "events": store.events(key)}) + "\n"
            )
        proposal_events = output / "proposals"
        proposal_events.mkdir()
        for key in proposals:
            (proposal_events / f"{key}.json").write_text(canonical(store.get_artifact(key)) + "\n")
        with store.connect() as db:
            proposal_runs = [
                r[0]
                for r in db.execute("SELECT DISTINCT run_id FROM events")
                if r[0] not in before_events and r[0] not in run_ids
            ]
        for key in proposal_runs:
            (proposal_events / f"{key}.json").write_text(
                canonical({"events": store.events(key), "cost": store.costs([key])}) + "\n"
            )
        receipts = output / "evaluations"
        receipts.mkdir()
        for item in timeline:
            if item.get("receipt"):
                (receipts / f"{item['receipt']}.json").write_text(
                    canonical(store.get_artifact(item["receipt"])) + "\n"
                )
    return report
