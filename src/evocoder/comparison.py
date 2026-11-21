"""Frozen four-arm evaluation, isolated knowledge states and one cumulative ledger."""

from __future__ import annotations

import fcntl
import json
import shutil
import sqlite3
from pathlib import Path

from evocoder.contracts import MODEL_TASK_VIEW, Settings, canonical, digest
from evocoder.dataset import load_catalog
from evocoder.evolution import Evolution
from evocoder.jobs import bounded_map
from evocoder.library import SkillLibrary
from evocoder.library_stream import learning_checkpoint, library_stream
from evocoder.memory import freeze_pool
from evocoder.reservations import audit_historical_reservations, open_calls
from evocoder.runtime import Runner
from evocoder.statistics import ARMS, summarize
from evocoder.store import Store, nanos, uid


def dump(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    temp.replace(path)


def sources():
    return {p.name: p.read_text() for p in sorted(Path(__file__).parent.glob("*.py"))}


def frozen_knowledge(store: Store):
    with store.connect() as db:
        return digest(
            {
                "skills": [tuple(r) for r in db.execute("SELECT * FROM skills ORDER BY id")],
                "active": store.active(),
                "library": store.active_library(),
                "releases": [tuple(r) for r in db.execute("SELECT * FROM library_releases ORDER BY seq")],
            }
        )


def revision_test_preflight(stores: dict[str, Store], settings: Settings) -> dict:
    members = {}
    for arm in ("C", "D"):
        library_id = stores[arm].active_library()
        members[arm] = set(
            stores[arm].library_members(library_id, mode=settings.mode, require_published=True) if library_id else []
        )
    replaced = members["C"] - members["D"]
    additions = members["D"] - members["C"]
    library = SkillLibrary(settings, stores["D"])
    replacements = {}
    for revision in sorted(additions):
        ancestors = library.provenance(revision)["members"] - {revision}
        matches = replaced & ancestors
        if len(matches) == 1:
            replacements[revision] = next(iter(matches))
    passed = (
        bool(replacements)
        and len(replacements) == len(replaced)
        and set(replacements) == additions
        and set(replacements.values()) == replaced
    )
    return {
        "passed": passed,
        "reason": None if passed else "no_comparable_published_revision",
        "revision_to_frozen_version": replacements,
    }


def clone_state(source: Store, target: Path, ledger: Store):
    if target.exists():
        raise ValueError("knowledge clone destination exists")
    target.mkdir(parents=True)
    with source.connect() as original:
        copy = sqlite3.connect(target / "evocoder.sqlite3")
        try:
            original.backup(copy)
        finally:
            copy.close()
    return Store(target, ledger=ledger)


def require_current_model_input(store: Store, run_ids: list[str]) -> None:
    for run_id in run_ids:
        start = next((event["payload"] for event in store.events(run_id) if event["kind"] == "START"), {})
        if start.get("model_task_view") != MODEL_TASK_VIEW:
            raise ValueError("reused executions predate controller metadata isolation; acquire fresh evidence")


def seed_snapshot(store: Store, settings: Settings, seed_tasks: list[str]) -> dict:
    library = SkillLibrary(settings, store)
    with store.connect() as db:
        roots = [tuple(r) for r in db.execute("SELECT id,parent FROM skills ORDER BY rowid")]
        evaluations = [tuple(r) for r in db.execute("SELECT * FROM library_evaluations ORDER BY id")]
        run_ids = [r[0] for r in db.execute("SELECT id FROM runs")]
    require_current_model_input(store, run_ids)
    if not roots or len(roots) > 3 or any(parent for _, parent in roots):
        raise ValueError("shared initial skills must be 1..3 unmodified roots")
    for key, _ in roots:
        if not set(library.provenance(key)["support"]) <= set(seed_tasks):
            raise ValueError("shared skill evidence is not from the predeclared seed tasks")
    return {
        "knowledge": frozen_knowledge(store),
        "root_ids": [key for key, _ in roots],
        "evaluations": evaluations,
    }


def state_snapshot(store: Store) -> str:
    with store.connect() as db:
        names = [
            r[0]
            for r in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
        return digest(
            {
                name: [
                    tuple(r)
                    for r in db.execute('SELECT * FROM "' + name.replace('"', '""') + '" ORDER BY rowid')
                ]
                for name in names
            }
        )


def continuation_snapshot(settings: Settings, previous: Path, ledger: Store) -> dict:
    """Audit a stopped, never-tested study without releasing its uncertain charge."""
    protocol = json.loads((previous / "protocol.json").read_text())
    result = json.loads((previous / "result.json").read_text())
    if (
        result["completed"]
        or result["phase"] not in {"learn_C", "learn_D"}
        or result["rows"]
        or result["protocol"] != protocol
        or list((previous / "cells").glob("*.json"))
        or result["budget_after"] != ledger.budget()
        or protocol["settings"]
        != settings.model_copy(update={"image": protocol["settings"]["image"]}).public()
    ):
        raise ValueError("continuation requires unchanged pre-test study and cumulative ledger")
    # Only orchestration/checkpoint code may differ. Solver, prompts, routing,
    # proposal rules, gates and scoring must be identical across retained/new runs.
    current = sources()
    old_source = {p.name: p.read_text() for p in (previous / "source").glob("*.py")}
    if digest(old_source) != protocol["source_hash"]:
        raise ValueError("retained source snapshot changed")
    orchestration = {"comparison.py", "library_stream.py", "cli.py"}
    if {k: v for k, v in current.items() if k not in orchestration} != {
        k: v for k, v in old_source.items() if k not in orchestration
    }:
        raise ValueError("solver or evaluation policy changed since retained learning")
    manifest = Path(protocol["manifest"])
    if (
        digest(json.loads(manifest.read_text())) != protocol["manifest_hash"]
        or digest(json.loads(Path(protocol["verification"]).read_text())) != protocol["verification_hash"]
    ):
        raise ValueError("retained dataset or verification changed")
    tasks = load_catalog(manifest)
    learn = [t for t in tasks if t.split == "learn" and t.id != protocol.get("pilot_seed")]
    arm = result["phase"][-1]
    folders = ["common", "B", "seed", "C"] + (["D"] if arm == "D" else [])
    if any(not (previous / name / "evocoder.sqlite3").is_file() for name in folders):
        raise ValueError("missing retained knowledge state")
    if arm == "C" and ((previous / "D").exists() or (previous / "learning-D").exists()):
        raise ValueError("D must not have started before interrupted C completes")
    states = {name: Store(previous / name, ledger=ledger) for name in folders}
    control = json.loads((previous / "learning-C" / "summary.json").read_text())
    if (
        (arm == "D" and not control["completed"])
        or control["proposal_attempts"] != 0
        or (arm == "D" and [r["task_id"] for r in control["timeline"]] != [t.id for t in learn])
        or control["timeline"] != json.loads((previous / "learning-C" / "timeline.json").read_text())
    ):
        raise ValueError("completed C learning checkpoint is missing or changed")
    for task, item in zip(learn, control["timeline"], strict=True) if arm == "D" else []:
        run = states["C"].run(item["run_id"])
        if (
            run["task_hash"] != digest(task.model_dump())
            or run["task_id"] != task.id
            or run["state"] != item["state"]
            or run["state"] not in {"COMPLETE", "FAILED", "EXHAUSTED"}
            or run["purpose"] != "solve"
            or run["split"] != "learn"
            or run["mode"] != settings.mode
        ):
            raise ValueError("C checkpoint differs from actual executions")
    checkpoint = learning_checkpoint(states[arm], manifest, previous / f"learning-{arm}", settings.mode)
    failed = checkpoint["timeline"][-1]["run_id"]
    known = {
        call["id"]: call
        for call in [
            *(protocol.get("historical_reservations") or {}).get("calls", []),
            *(protocol.get("continuation") or {}).get("preserved_reservations", []),
        ]
    }
    test_ids = {t.id for t in tasks if t.split == "test"}
    test_hashes = {digest(t.model_dump()) for t in tasks if t.split == "test"}
    with ledger.connect() as db:
        unresolved = [dict(r) for r in db.execute("SELECT * FROM calls WHERE state='reserved' ORDER BY id")]
        newly_unresolved = [
            call for call in unresolved
            if call["id"] not in known
            or any(call[key] != known[call["id"]][key] for key in ("run_id", "reserved"))
        ]
        if (
            len(newly_unresolved) != 1
            or newly_unresolved[0]["run_id"] != failed
            or any(
                r["task_id"] in test_ids or r["task_hash"] in test_hashes
                for r in db.execute("SELECT task_id,task_hash FROM test_exposures")
            )
        ):
            raise ValueError("unexpected unresolved call or final-test exposure; continuation refused")
    for state in states.values():
        with state.connect() as db:
            if db.execute("SELECT COUNT(*) FROM runs WHERE state='RUNNING' OR split='test'").fetchone()[0]:
                raise ValueError("unresolved execution or prior test in checkpoint")
    return {
        "from": str(previous.resolve()),
        "protocol_hash": digest(protocol),
        "result_hash": digest(result),
        "state_hashes": {name: state_snapshot(state) for name, state in states.items()},
        "control_summary_hash": digest(control),
        "resume_arm": arm,
        "checkpoint_hash": digest(checkpoint),
        "retained_tasks": len(checkpoint["timeline"]),
        "preserved_reservations": unresolved,
        "failed_task_policy": "retain ERROR, do not retry; continue next learning task",
    }


def prepare_continuation(settings: Settings, previous: Path, root: Path):
    protocol = json.loads((previous / "protocol.json").read_text())
    return prepare(
        settings,
        Path(protocol["manifest"]),
        Path(protocol["verification"]),
        root,
        repeats=protocol["repeats"],
        workers=protocol["workers"],
        history_from=previous / "common",
        seed_from=previous / "seed",
        continuation_from=previous,
        pilot_seed=protocol.get("pilot_seed"),
    )


def prepare(
    settings: Settings,
    manifest: Path,
    verification: Path,
    root: Path,
    *,
    repeats=3,
    workers=4,
    history_from: Path | None = None,
    seed_from: Path | None = None,
    continuation_from: Path | None = None,
    replication_from: Path | None = None,
    carry_reservations_from: list[Path] | None = None,
    pilot_seed: str | None = None,
):
    if root.exists():
        raise ValueError("experiment destination must be new")
    settings.validate_live()
    if settings.mode != "live" or settings.skill_routing != "semantic":
        raise ValueError("real comparison requires live mode and semantic routing")
    if repeats < 1 or repeats > 5 or workers < 1 or workers > 4:
        raise ValueError("repeats 1..5 and workers 1..4 required")
    tasks = load_catalog(manifest)
    counts = {split: sum(t.split == split for t in tasks) for split in ("learn", "dev", "test")}
    if pilot_seed is not None:
        if any(not 2 <= count <= 8 for count in counts.values()) or len({task.repository for task in tasks}) != 1:
            raise ValueError("pilot needs 2..8 tasks per split from a single repository")
        if pilot_seed not in {task.id for task in tasks if task.split == "learn"}:
            raise ValueError("pilot seed must be a predeclared learn task")
        if len({task.source_group for task in tasks if task.split == "dev"}) < 2:
            raise ValueError("pilot needs at least two distinct dev groups")
    elif counts != {"learn": 32, "dev": 16, "test": 32}:
        raise ValueError("formal exploratory protocol requires 32/16/32 distinct tasks")
    data = json.loads(manifest.read_text())
    proof = json.loads(verification.read_text())
    if (
        proof.get("passed") is not True
        or proof["manifest_hash"] != digest(data)
        or {r["id"]: r["task_hash"] for r in proof["rows"] if r["passed"]}
        != {t.id: digest(t.model_dump()) for t in tasks}
    ):
        raise ValueError("offline defect/reference verification is incomplete or stale")
    unit = settings.accounting_unit
    ledger = Store(settings.state_dir, unit=unit)
    if unit == "credits" and not ledger.budget()["configured"]:
        ledger.configure_budget(
            (1 << 63) - 1 if settings.unlimited_credits else nanos(settings.credit_budget)
        )
    continuation = None
    if continuation_from:
        continuation = continuation_snapshot(settings, continuation_from, ledger)
        if history_from != continuation_from / "common" or seed_from != continuation_from / "seed":
            raise ValueError("continuation must preserve its exact historical and initial skill states")
    if ledger.budget()["blocked"]:
        raise ValueError("ledger accounting is blocked")
    historical_reservations = None
    if not continuation:
        historical_reservations = audit_historical_reservations(ledger, carry_reservations_from or [])
    expected_cap = (
        None if unit == "credits" and settings.unlimited_credits else settings.accounting_rates()[0]
    )
    if ledger.budget()["cap_" + unit] != expected_cap:
        raise ValueError("configuration differs from authorized cumulative cap")
    with ledger.connect() as db:
        seen_ids = {r[0] for r in db.execute("SELECT DISTINCT task_id FROM runs")}
        seen_hashes = {r[0] for r in db.execute("SELECT DISTINCT task_hash FROM runs")}
        for row in db.execute("SELECT task_id,task_hash FROM test_exposures"):
            seen_ids.add(row[0])
            seen_hashes.add(row[1])
    if any(t.split == "test" and (t.id in seen_ids or digest(t.model_dump()) in seen_hashes) for t in tasks):
        raise ValueError("final test overlaps previously executed task IDs or snapshots")
    # Root seed selection is declared before any model outcome; missing runs are not replaced.
    if pilot_seed is not None:
        seeds = [pilot_seed]
    else:
        seed_repositories = ("tkem/cachetools", "more-itertools/more-itertools", "pallets/click")
        seeds = [
            next(t.id for t in tasks if t.split == "learn" and t.repository == repo) for repo in seed_repositories
        ]
    seed_hash = None
    if seed_from:
        if not history_from:
            raise ValueError("reused initial skills require their explicit historical corpus")
        seed_hash = digest(seed_snapshot(Store(seed_from, ledger=ledger), settings, seeds))
    config = settings.model_copy(update={"image": proof["image_id"]})
    history_hash = None
    if history_from:
        historical = Store(history_from, ledger=ledger)
        with historical.connect() as db:
            ids = [r[0] for r in db.execute("SELECT id FROM runs ORDER BY task_id")]
            if db.execute("SELECT COUNT(*) FROM skills").fetchone()[0]:
                raise ValueError("shared history must not contain skill proposals")
        require_current_model_input(historical, ids)
        historical_runs = [historical.run(r) for r in ids]
        if any(
            r["split"] != "learn" or r["purpose"] != "solve" or r["mode"] != settings.mode
            for r in historical_runs
        ) or {r["task_id"]: r["task_hash"] for r in historical_runs} != {
            t.id: digest(t.model_dump()) for t in tasks if t.split == "learn"
        }:
            raise ValueError("shared history does not match the exact learning snapshots")
        history_hash = digest(historical_runs)
    protocol = {
        "schema": 1,
        "billing_unit": unit,
        "claim": "single_family_pilot" if pilot_seed is not None else "exploratory_historical_commit_defects",
        "pilot_seed": pilot_seed,
        "manifest": str(manifest.resolve()),
        "verification": str(verification.resolve()),
        "manifest_hash": digest(data),
        "verification_hash": digest(proof),
        "source_hash": digest(sources()),
        "model_task_view": MODEL_TASK_VIEW,
        "settings": config.public(),
        "budget_before": ledger.budget(),
        "history_from": str(history_from.resolve()) if history_from else None,
        "history_run_hash": history_hash,
        "seed_from": str(seed_from.resolve()) if seed_from else None,
        "seed_snapshot_hash": seed_hash,
        "arms": {
            "A": "no experience",
            "B": "public-trajectory TF-IDF RAG",
            "C": "shared initial skills, revisions disabled",
            "D": "same initial skills, feedback revisions enabled",
        },
        "root_seed_task_ids": seeds,
        "common_evidence": "same frozen no-experience learn executions in all states",
        "primary_contrast": "D_vs_C",
        "repeats": repeats,
        "workers": workers,
        "max_revision_proposals": 1 if pilot_seed is not None else 3,
        "feedback_policy": "learn_outcome",
        "require_revised_library_for_test": True,
        "learning_orders": 1,
        "learning_order": (
            "predeclared non-seed learn tasks in catalog order"
            if pilot_seed is not None else "manifest chronological order within each repository"
        ),
        "test_order": "task/repeat grid, rotated arm order; bounded concurrent dispatch",
        "memory_budget_bytes": settings.max_memory_bytes,
        "test_learning": False,
        "bootstrap": {
            "samples": 4000,
            "seed": 20261117,
            "unit": "source_group; task means over repeated solves",
        },
        "limitations": [
            "historical commit-message prompts, not original issue texts",
            "public upstream data may be in model pretraining",
            "same repositories across splits; not cross-repository generalization",
            "one learning order; no task-order robustness claim",
            "API model alias fixed, actual provider weights are not independently verifiable",
        ],
    }
    if historical_reservations:
        protocol["historical_reservations"] = historical_reservations
    if pilot_seed is not None:
        protocol["limitations"].append("small single-repository pilot for diagnosis; undersized for a positive claim")
    if continuation:
        protocol["continuation"] = continuation
        protocol["limitations"].append(
            f"one interrupted {continuation['resume_arm']} learning task retained as ERROR without retry; "
            "uncertain charge remains reserved and learning continues with the next task"
        )
        prior_protocol = json.loads((continuation_from / "protocol.json").read_text())
        protocol["feedback_policy"] = prior_protocol.get("feedback_policy", "post_edit_or_budget")
        protocol["require_revised_library_for_test"] = prior_protocol.get("require_revised_library_for_test", False)
        if "cross_model_replication" in prior_protocol:
            protocol["cross_model_replication"] = prior_protocol["cross_model_replication"]
            protocol["limitations"].append(
                "some test tasks previously evaluated by controller on another model; "
                "cross-model replication, not controller-blind"
            )
    if replication_from:
        prior = json.loads((replication_from / "result.json").read_text())
        if prior["protocol"]["manifest_hash"] != protocol["manifest_hash"]:
            raise ValueError("cross-model replication requires the same frozen benchmark")
        protocol["cross_model_replication"] = {
            "from": str(replication_from.resolve()),
            "prior_result_hash": digest(prior),
            "prior_model": prior["protocol"]["settings"]["model"],
            "prior_cells": len(prior["rows"]),
            "previously_evaluated_test_ids": sorted({r["task_id"] for r in prior["rows"]}),
            "reuse_previous_test_results_or_trajectories": False,
        }
        protocol["limitations"].append(
            "some test tasks previously evaluated by controller on another model; cross-model replication, not controller-blind"
        )
    root.mkdir(parents=True)
    dump(root / "protocol.json", protocol)
    for name, body in sources().items():
        path = root / "source" / name
        path.parent.mkdir(exist_ok=True)
        path.write_text(body)
    return protocol


def collect_test_rows(root: Path) -> list[dict]:
    rows = [json.loads(path.read_text()) for path in sorted((root / "cells").glob("*.json"))]
    return [row for row in rows if row["state"] != "started"]


def evaluate_test_matrix(
    options: Settings, root: Path, protocol: dict, ledger: Store, stores, test, frozen, *, resume=False,
):
    """Run the never-started cells and reconcile earlier intents from their run IDs."""
    fingerprints = frozen["knowledge_hashes"]
    library_ids = frozen["library_ids"]
    pool = frozen["experience_pool"]
    if any(frozen_knowledge(store) != fingerprints[arm] for arm, store in stores.items()):
        raise ValueError("test knowledge changed after freeze")
    if library_ids != {arm: store.active_library() for arm, store in stores.items()}:
        raise ValueError("test library differs from the frozen publication")
    stores["A"].get_artifact(pool)  # Verify the content hash before dispatch.
    cost_key, reserved_key = "cost_" + ledger.unit, "reserved_" + ledger.unit

    def read_run(task, repeat, arm, run_id):
        store = stores[arm]
        run = store.run(run_id)
        if (
            run["task_id"] != task.id or run["task_hash"] != digest(task.model_dump())
            or run["mode"] != options.mode or run["purpose"] != "evaluate" or run["split"] != "test"
            or run["state"] not in {"COMPLETE", "FAILED", "EXHAUSTED", "ERROR", "INTERRUPTED"}
        ):
            raise ValueError("test cell does not match its terminal execution")
        outcome = run["result"]
        return {
            "cost_unit": ledger.unit,
            "task_id": task.id,
            "source_group": task.source_group,
            "repository": task.repository,
            "repeat": repeat,
            "arm": arm,
            "run_id": run_id,
            "passed": outcome["passed"],
            "state": run["state"],
            cost_key: outcome["cost"][cost_key] if "cost" in outcome else store.costs([run_id])[cost_key],
            "model_steps": outcome.get("model_steps"),
            "applied_skill_id": run["skill_id"],
            "library_id": library_ids[arm],
            "experience_pool_id": pool if arm == "B" else None,
        }

    schedule = {}
    for index, task in enumerate(test):
        for repeat in range(protocol["repeats"]):
            shift = (index + repeat) % len(ARMS)
            for arm in ARMS[shift:] + ARMS[:shift]:
                path = root / "cells" / f"{task.id}-{repeat}-{arm}.json"
                schedule[path] = (task, repeat, arm)

    started = {}
    existing = []
    for path in (root / "cells").glob("*.json"):
        if path not in schedule:
            raise ValueError("unexpected test cell in checkpoint")
        task, repeat, arm = schedule[path]
        record = json.loads(path.read_text())
        run_id = record.get("run_id")
        if not run_id or run_id in started or (record["task_id"], record["repeat"], record["arm"]) != (
            task.id, repeat, arm,
        ):
            raise ValueError("test intent has a missing, duplicate, or mismatched run ID")
        started[run_id] = path
        existing.append((path, task, repeat, arm, run_id, record))
    if existing and not resume:
        raise ValueError("final evaluation already has test intents")
    for arm, store in stores.items():
        with store.connect() as db:
            for row in db.execute("SELECT id FROM runs WHERE split='test'"):
                path = started.get(row["id"])
                if path is None or schedule[path][2] != arm:
                    raise ValueError("recorded final execution has no matching test intent")
    retained_calls = {call["id"]: call for call in frozen["open_calls"]}
    for call in open_calls(ledger):
        if call["run_id"] not in started and retained_calls.get(call["id"]) != call:
            raise ValueError("new untracked reservation on the test ledger")
    if ledger.budget()["blocked"]:
        raise ValueError("test ledger accounting is blocked")

    for path, task, repeat, arm, run_id, record in existing:
        if record["state"] == "started":
            with stores[arm].connect() as db:
                row = db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
            if row is None:
                continue
            if row["state"] == "RUNNING":
                if (
                    row["task_id"] != task.id or row["task_hash"] != digest(task.model_dump())
                    or row["split"] != "test" or row["purpose"] != "evaluate" or row["mode"] != options.mode
                ):
                    raise ValueError("unfinished run does not match the test intent")
                # The original comparison no longer holds the ledger lock.
                stores[arm].finish(run_id, "INTERRUPTED", {
                    "passed": False,
                    "error": "comparison stopped before the test run reached a terminal state",
                    "cost": stores[arm].costs([run_id]),
                })
            dump(path, read_run(task, repeat, arm, run_id))
        elif read_run(task, repeat, arm, run_id) != record:
            raise ValueError("recorded test cell differs from its execution")

    protocol_hash = digest(protocol)

    def trial(path):
        task, repeat, arm = schedule[path]
        store = stores[arm]
        config = options.model_copy(update={"state_dir": store.root})
        run_id = uid()
        dump(path, {"task_id": task.id, "repeat": repeat, "arm": arm, "state": "started", "run_id": run_id})
        ledger.claim_test(task.id, digest(task.model_dump()), protocol_hash)
        if Runner(config, store).solve(
            task, use_active=False, purpose="evaluate", run_id=run_id,
            library_id=library_ids[arm], experience_pool_id=pool if arm == "B" else None,
        ) != run_id:
            raise RuntimeError("runner returned a different test intent ID")
        row = read_run(task, repeat, arm, run_id)
        dump(path, row)
        if store.costs([run_id])[reserved_key]:
            raise RuntimeError("unsettled trial response; no more dispatch")
        if ledger.budget()["blocked"] or row["state"] in {"ERROR", "INTERRUPTED"}:
            raise RuntimeError("test execution stopped; inspect the retained run")
        return row

    def progressed(_row, _index):
        if any(frozen_knowledge(store) != fingerprints[arm] for arm, store in stores.items()):
            raise RuntimeError("test altered frozen knowledge")
        dump(root / "progress.json", collect_test_rows(root))

    pending = [path for path in schedule if not path.exists()]
    bounded_map(trial, pending, protocol["workers"], progressed)


def finish_comparison(root: Path, result: dict, test, ledger: Store, phases: list) -> None:
    protocol = result["protocol"]
    rows = collect_test_rows(root)
    stats = summarize(rows, [task.id for task in test], protocol["repeats"], cost_unit=ledger.unit)
    budget = ledger.budget()
    spent = "spent_" + ledger.unit
    intents = sum(
        json.loads(path.read_text())["state"] == "started" for path in (root / "cells").glob("*.json")
    )
    result.update(
        rows=rows, phases=phases, budget_after=budget, statistics=stats,
        started_without_terminal_record=intents,
    )
    result["new_cost_" + ledger.unit] = round(budget[spent] - protocol["budget_before"][spent], 12)
    result["completed"] = result["phase"] == "test" and stats["valid_complete"] and "error" not in result
    if result["completed"]:
        result["phase"] = "complete"
    dump(root / "result.json", result)


def resume_test_matrix(options: Settings, root: Path, protocol: dict, ledger: Store, test):
    checkpoint = json.loads((root / "frozen-test-state.json").read_text())
    started = json.loads((root / "started.json").read_text())
    if started.get("protocol_hash") != digest(protocol) or checkpoint.get("protocol_hash") != digest(protocol):
        raise ValueError("test resumption requires the original frozen protocol and checkpoint")
    if ledger.budget()["cap_" + ledger.unit] != protocol["budget_before"]["cap_" + ledger.unit]:
        raise ValueError("cumulative budget cap changed since protocol freeze")
    previous_file = root / "result.json"
    previous = json.loads(previous_file.read_text()) if previous_file.is_file() else {}
    if previous and (previous["completed"] or previous["phase"] != "test" or previous["protocol"] != protocol):
        raise ValueError("only an interrupted final evaluation can resume")
    arms = {"A": "common", "B": "B", "C": "C", "D": "D"}
    if any(not (root / name / "evocoder.sqlite3").is_file() for name in arms.values()):
        raise ValueError("incomplete final evaluation state")
    stores = {arm: Store(root / name, ledger=ledger) for arm, name in arms.items()}
    preflight = checkpoint["test_preflight"]
    if protocol.get("require_revised_library_for_test") and (
        not preflight["passed"] or revision_test_preflight(stores, options) != preflight
    ):
        raise ValueError("final evaluation preflight no longer matches")
    result = {
        "protocol": protocol,
        "completed": False,
        "phase": "test",
        "test_preflight": preflight,
        "resumptions": [
            *previous.get("resumptions", []),
            {"previous_result_hash": digest(previous) if previous else None, "stop_error": previous.get("error")},
        ],
    }
    try:
        evaluate_test_matrix(options, root, protocol, ledger, stores, test, checkpoint, resume=True)
    except (KeyboardInterrupt, SystemExit):
        result["error"] = "interrupted; previously started cells are retained"
        raise
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {str(exc)[:1500]}".replace(options.api_key, "[redacted]")
    finally:
        finish_comparison(root, result, test, ledger, checkpoint["phases"])
    return result


def execute(settings: Settings, root: Path, *, resume_test=False):
    ledger = Store(settings.state_dir, unit=settings.accounting_unit)
    with (ledger.root / "comparison.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("another comparison is using this ledger") from None
        return _execute(settings, root, ledger, resume_test=resume_test)


def _execute(settings: Settings, root: Path, ledger: Store, *, resume_test: bool):
    unit = settings.accounting_unit
    spent_key, cost_key = "spent_" + unit, "cost_" + unit
    protocol = json.loads((root / "protocol.json").read_text())
    options = Settings.model_validate({**protocol["settings"], "api_key": settings.api_key})
    options.validate_live()
    if digest(sources()) != protocol["source_hash"]:
        raise ValueError("source changed after protocol freeze")
    current = settings.model_copy(update={"image": options.image})
    if current.public() != options.public():
        raise ValueError("configuration changed after protocol freeze")
    manifest = Path(protocol["manifest"])
    if digest(json.loads(manifest.read_text())) != protocol["manifest_hash"]:
        raise ValueError("task manifest changed")
    if digest(json.loads(Path(protocol["verification"]).read_text())) != protocol["verification_hash"]:
        raise ValueError("offline verification changed")
    tasks = load_catalog(manifest)
    learn, dev, test = ([t for t in tasks if t.split == s] for s in ("learn", "dev", "test"))
    if resume_test:
        return resume_test_matrix(options, root, protocol, ledger, test)
    if ledger.budget() != protocol["budget_before"]:
        raise ValueError("ledger changed after protocol freeze")
    history = protocol.get("historical_reservations")
    if history is not None and audit_historical_reservations(
        ledger, [Path(path) for path in history["origins"]]
    ) != history:
        raise ValueError("historical reservation evidence changed after protocol freeze")
    with (root / "started.json").open("x") as started:
        started.write(canonical({
            "budget": ledger.budget(), "source_hash": protocol["source_hash"], "protocol_hash": digest(protocol),
        }))
    phases = []
    result = {"protocol": protocol, "completed": False, "phase": "acquire"}
    stores = {}
    try:
        continuation = protocol.get("continuation")
        if continuation:
            previous = Path(continuation["from"])
            if continuation_snapshot(options, previous, ledger) != continuation:
                raise ValueError("continuation checkpoint changed after freeze")
            resumed_arm = continuation["resume_arm"]
            copies = [("A", "common"), ("B", "B"), ("C", "C")]
            if resumed_arm == "D":
                copies.append(("D", "D"))
            for arm, folder in copies:
                stores[arm] = clone_state(Store(previous / folder, ledger=ledger), root / folder, ledger)
            seed_store = clone_state(Store(previous / "seed", ledger=ledger), root / "seed", ledger)
            seed_ids = seed_snapshot(seed_store, options, protocol["root_seed_task_ids"])["root_ids"]
            for name in ["acquisition.json", "experience-pool.json", "seed-reports.json"]:
                shutil.copy2(previous / name, root / name)
            if resumed_arm == "D":
                shutil.copytree(previous / "learning-C", root / "learning-C")
            frozen_pool = json.loads((root / "experience-pool.json").read_text())
            pool = frozen_pool["id"]
            common = stores["A"]
            acquisition = json.loads((root / "acquisition.json").read_text())
            valid = [r["id"] for r in acquisition if r["state"] in {"COMPLETE", "FAILED", "EXHAUSTED"}]
            frozen_ids = [d["run_id"] for d in frozen_pool["artifact"]["documents"]]
            if (
                set(frozen_ids) != set(valid)
                or common.get_artifact(pool) != frozen_pool["artifact"]
                or freeze_pool(common, frozen_ids, options.mode) != pool
            ):
                raise ValueError("continuation changed the frozen public evidence pool")
            phases.extend(
                [
                    {"phase": name, cost_key: 0.0, "reused": True}
                    for name in ["common_acquisition", "shared_initial_skills"]
                    + (["learning_C"] if resumed_arm == "D" else [])
                ]
            )
        else:
            if protocol.get("history_from"):
                historical = Store(Path(protocol["history_from"]), ledger=ledger)
                with historical.connect() as db:
                    historical_runs = [
                        historical.run(r[0]) for r in db.execute("SELECT id FROM runs ORDER BY task_id")
                    ]
                if digest(historical_runs) != protocol["history_run_hash"]:
                    raise ValueError("shared historical executions changed after freeze")
                require_current_model_input(historical, [r["id"] for r in historical_runs])
                common = clone_state(historical, root / "common", ledger)
            else:
                common = Store(root / "common", ledger=ledger)
            runner = Runner(options.model_copy(update={"state_dir": common.root}), common)
            learning_runs = []
            acquisition_before = ledger.budget()[spent_key]

            def acquire(task):
                run_id = runner.solve(task, use_active=False)
                if common.costs([run_id])["reserved_" + unit] or ledger.budget()["blocked"]:
                    raise RuntimeError("ambiguous response or sealed budget; stopping without HTTP retry")
                return run_id

            def acquired(run_id, index):
                learning_runs.append(run_id)
                dump(root / "acquisition.json", [common.run(r) for r in learning_runs])

            if protocol.get("history_from"):
                by_task = {r["task_id"]: r["id"] for r in historical_runs}
                learning_runs = [by_task[t.id] for t in learn]
                dump(root / "acquisition.json", [common.run(r) for r in learning_runs])
            else:
                learning_runs = bounded_map(acquire, learn, protocol["workers"], acquired)
            phases.append(
                {"phase": "common_acquisition", cost_key: ledger.budget()[spent_key] - acquisition_before}
            )
            valid = [
                r for r in learning_runs if common.run(r)["state"] in {"COMPLETE", "FAILED", "EXHAUSTED"}
            ]
            pool = freeze_pool(common, valid, options.mode)
            dump(root / "experience-pool.json", {"id": pool, "artifact": common.get_artifact(pool)})
            stores["A"] = common
            stores["B"] = clone_state(common, root / "B", ledger)
            seed_before = ledger.budget()[spent_key]
            if protocol.get("seed_from"):
                seed_source = Store(Path(protocol["seed_from"]), ledger=ledger)
                snapshot = seed_snapshot(seed_source, options, protocol["root_seed_task_ids"])
                if digest(snapshot) != protocol["seed_snapshot_hash"]:
                    raise ValueError("initial seed state changed after protocol freeze")
                seed_store = clone_state(seed_source, root / "seed", ledger)
                seed_ids = snapshot["root_ids"]
                with seed_store.connect() as db:
                    seed_reports = [
                        {"receipt": r[0], **json.loads(r[1]), "reused": True}
                        for r in db.execute("SELECT id,report FROM library_evaluations ORDER BY rowid")
                    ]
                dump(root / "seed-reports.json", seed_reports)
            else:
                seed_store = clone_state(common, root / "seed", ledger)
                seed_settings = options.model_copy(update={"state_dir": seed_store.root})
                evolution = Evolution(seed_settings, seed_store, feedback_policy="post_edit_or_budget")
                library = SkillLibrary(seed_settings, seed_store, Runner(seed_settings, seed_store))
                seed_ids = []
                seed_before = ledger.budget()[spent_key]
                by_task = {common.run(r)["task_id"]: r for r in valid}
                seed_reports = []
                for task_id in protocol["root_seed_task_ids"]:
                    if task_id not in by_task:
                        seed_reports.append(
                            {
                                "task_id": task_id,
                                "skipped": "predeclared seed execution invalid; no replacement",
                            }
                        )
                        continue
                    key = evolution.propose([by_task[task_id]])
                    seed_ids.append(key)
                    required = set()
                    for member in [*library.members(seed_store.active_library()), key]:
                        required.update(library.provenance(member)["support"])
                    before = seed_store.active_library()
                    report = library.evaluate(
                        key, dev, [t for t in learn if t.id in required], workers=protocol["workers"]
                    )
                    seed_reports.append(report)
                    if report["gate"]["passed"]:
                        seed_store.activate_library(
                            report["library_id"], report["receipt"], before, mode=options.mode
                        )
                    dump(root / "seed-reports.json", seed_reports)
            phases.append(
                {"phase": "shared_initial_skills", cost_key: ledger.budget()[spent_key] - seed_before}
            )
        for arm in ("D",) if continuation and resumed_arm == "D" else ("C", "D"):
            if arm not in stores:
                stores[arm] = clone_state(seed_store, root / arm, ledger)
            arm_settings = options.model_copy(update={"state_dir": stores[arm].root})
            before = ledger.budget()[spent_key]
            result["phase"] = "learn_" + arm
            report = library_stream(
                arm_settings,
                stores[arm],
                manifest,
                root / f"learning-{arm}",
                initial_skills=seed_ids,
                feedback_policy=protocol.get("feedback_policy", "post_edit_or_budget"),
                allow_revisions=arm == "D",
                allow_new_families=False,
                max_proposals=protocol["max_revision_proposals"],
                evaluation_workers=protocol["workers"],
                tolerate_rejected_proposals=True,
                skip_seed_task=protocol.get("pilot_seed"),
                continuation_from=(
                    previous / f"learning-{arm}" if continuation and arm == resumed_arm else None
                ),
            )
            phases.append(
                {
                    "phase": "learning_" + arm,
                    cost_key: ledger.budget()[spent_key] - before,
                    "completed": report["completed"],
                    "proposals": report["proposal_count"],
                }
            )
            if not report["completed"]:
                raise RuntimeError(f"{arm} learning did not complete: " + str(report["error"]))
        if protocol.get("require_revised_library_for_test", False):
            preflight = revision_test_preflight(stores, options)
            result["test_preflight"] = preflight
            if not preflight["passed"]:
                result["phase"] = "no_revised_library"
                return result
        initial_reservations = [
            *(protocol.get("continuation") or {}).get("preserved_reservations", []),
            *(protocol.get("historical_reservations") or {}).get("calls", []),
        ]
        known = {call["id"]: call for call in initial_reservations}
        outstanding = open_calls(ledger)
        if any(
            candidate["id"] not in known
            or any(known[candidate["id"]][key] != value for key, value in candidate.items())
            for candidate in outstanding
        ):
            raise RuntimeError("new untracked reservation before final evaluation")
        frozen = {
            "protocol_hash": digest(protocol),
            "knowledge_hashes": {arm: frozen_knowledge(store) for arm, store in stores.items()},
            "library_ids": {arm: store.active_library() for arm, store in stores.items()},
            "experience_pool": pool,
            "budget": ledger.budget(),
            "open_calls": outstanding,
            "phases": phases,
            "test_preflight": result.get("test_preflight"),
        }
        dump(root / "frozen-test-state.json", frozen)
        result["phase"] = "test"
        evaluate_test_matrix(options, root, protocol, ledger, stores, test, frozen)
    except (KeyboardInterrupt, SystemExit):
        result["error"] = "interrupted; unfinished cell intents are not retried automatically"
        raise
    except Exception as exc:
        message = f"{type(exc).__name__}: {str(exc)[:1500]}"
        result["error"] = message.replace(options.api_key, "[redacted]")
    finally:
        finish_comparison(root, result, test, ledger, phases)
    return result
