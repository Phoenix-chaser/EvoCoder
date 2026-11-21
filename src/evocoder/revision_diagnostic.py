"""Compare revisions of a pilot skill without publishing either draft."""

import fcntl
import json
import sqlite3
from contextlib import closing
from pathlib import Path
from threading import Event

from evocoder.comparison import (
    dump,
    frozen_knowledge,
    require_current_model_input,
    seed_snapshot,
    sources,
    state_snapshot,
)
from evocoder.contracts import MODEL_TASK_VIEW, Settings, Task, digest
from evocoder.dataset import load_catalog
from evocoder.errors import ProposalRejected
from evocoder.evolution import Evolution
from evocoder.library import SkillLibrary
from evocoder.reservations import audit_historical_reservations
from evocoder.runtime import Runner
from evocoder.store import Store


def read(path: Path):
    return json.loads(path.read_text())


def task_fingerprints(tasks: list[Task]) -> dict[str, str]:
    return {task.id: digest(task.model_dump()) for task in tasks}


def check_fresh_holdouts(tasks: list[Task], history: Path, ledger: Store, *, label="holdout") -> int:
    if not tasks:
        return 0
    fingerprints = task_fingerprints(tasks)
    ids = set(fingerprints)
    hashes = set(fingerprints.values())
    states = {*history.rglob("evocoder.sqlite3"), ledger.db_path}
    for path in sorted(states):
        wal = path.with_name(path.name + "-wal")
        immutable = "" if wal.exists() and wal.stat().st_size else "&immutable=1"
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro" + immutable, uri=True)) as db:
            tables = {name for (name,) in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            for table in ("runs", "test_exposures"):
                if table not in tables:
                    continue
                for task_id, task_hash in db.execute(f"SELECT DISTINCT task_id,task_hash FROM {table}"):
                    if task_id in ids or task_hash in hashes:
                        raise ValueError(f"{label} {task_id} overlaps a prior run or exposure in {path}")
    return len(states)


def validate_cohort(
    manifest: Path, proof: Path, seed_task: str, seed_fingerprint: str, image: str, *, max_dev_tasks: int = 4,
) -> list[Task]:
    data, receipt = read(manifest), read(proof)
    tasks = load_catalog(manifest)
    fingerprints = task_fingerprints(tasks)
    rows = receipt["rows"]
    if (
        receipt.get("model_calls") != 0
        or receipt.get("passed") is not True
        or receipt["manifest_hash"] != digest(data)
        or receipt["image_id"] != image
        or len(rows) != len(tasks)
        or {r["id"]: r["task_hash"] for r in rows if r["passed"] and r["image_id"] == image} != fingerprints
    ):
        raise ValueError("new cohort needs complete original/reference verification on the frozen image")
    if data["seed_task_id"] != seed_task or fingerprints.get(seed_task) != seed_fingerprint:
        raise ValueError("new cohort must retain the exact original seed task")
    for row in rows:
        if row["id"] == seed_task:
            continue
        public = row["checks"]["buggy_public"]
        if public["exit_code"] != 1 or not public["junit"]["failures"] or public["junit"]["errors"]:
            raise ValueError("each new task needs a runnable public reproduction on the original source")
    counts = {split: sum(task.split == split for task in tasks) for split in ("learn", "dev", "test")}
    if (
        not 3 <= counts["learn"] <= 5
        or not 2 <= counts["dev"] <= max_dev_tasks
        or not 2 <= counts["test"] <= 4
        or len({task.repository for task in tasks}) != 1
        or any(len({task.source_group for task in tasks if task.split == split}) < 2 for split in ("dev", "test"))
    ):
        raise ValueError(f"new cohort needs 2..4 follow-ups, 2..{max_dev_tasks} dev and 2..4 test tasks with independent groups")
    return tasks


def copy_closed_seed(previous: Path, output: Path, ledger: Store) -> Store:
    source_db = previous / "seed" / "evocoder.sqlite3"
    wal = source_db.with_name(source_db.name + "-wal")
    if not source_db.is_file() or (wal.exists() and wal.stat().st_size):
        raise ValueError("the seed must have a closed, checkpointed database")
    state_dir = output / "seed"
    output.mkdir(parents=True)
    state_dir.mkdir()
    with (
        closing(sqlite3.connect(source_db.as_uri() + "?mode=ro&immutable=1", uri=True)) as source,
        closing(sqlite3.connect(state_dir / source_db.name)) as target,
    ):
        source.backup(target)
    return Store(state_dir, ledger=ledger)


def freeze_protocol(output: Path, protocol: dict) -> dict:
    source = sources()
    protocol["source_hash"] = digest(source)
    source_dir = output / "source"
    source_dir.mkdir()
    for name, body in source.items():
        (source_dir / name).write_text(body)
    dump(output / "protocol.json", protocol)
    return protocol


def prepare(
    settings: Settings, previous: Path, output: Path, *, manifest: Path | None = None, verification: Path | None = None,
) -> dict:
    previous = previous.resolve()
    prior_result = read(previous / "result.json")
    prior = read(previous / "protocol.json")
    if (
        prior_result["protocol"] != prior
        or prior["claim"] != "single_family_pilot"
        or prior["model_task_view"] != MODEL_TASK_VIEW
        or prior_result["completed"]
        or prior_result["rows"]
        or any((previous / name).exists() for name in ("learning-C", "learning-D", "cells"))
    ):
        raise ValueError("diagnostic requires a stopped clean-input pilot before C/D and holdout evaluation")
    options = settings.model_copy(update={"image": prior["settings"]["image"]})
    options.validate_live()
    if options.mode != "live" or options.public() != prior["settings"]:
        raise ValueError("use the pilot's frozen live model, solver budget, and ledger")

    if (manifest is None) != (verification is None):
        raise ValueError("provide both a new manifest and its verification receipt")
    original_manifest = Path(prior["manifest"])
    original_proof = Path(prior["verification"])
    if (
        digest(read(original_manifest)) != prior["manifest_hash"]
        or digest(read(original_proof)) != prior["verification_hash"]
    ):
        raise ValueError("the original task snapshots or verification receipt changed")
    original_tasks = load_catalog(original_manifest)
    seed_task = prior["pilot_seed"]
    if manifest is not None:
        manifest, proof = manifest.resolve(), verification.resolve()
        seed_fingerprint = task_fingerprints(original_tasks)[seed_task]
        tasks = validate_cohort(manifest, proof, seed_task, seed_fingerprint, options.image)
        holdouts = [task for task in tasks if task.split == "test"]
    else:
        manifest, proof = original_manifest, original_proof
        tasks, holdouts = original_tasks, []
    follow_up = [task.id for task in tasks if task.split == "learn" and task.id != seed_task]
    dev = [task.id for task in tasks if task.split == "dev"]
    if verification is None and (len(follow_up) != 2 or len(dev) != 2):
        raise ValueError("this diagnosis is bounded to two follow-up learn tasks and two dev tasks")

    ledger = Store(options.state_dir, unit=options.accounting_unit)
    freshness_databases = check_fresh_holdouts(holdouts, previous.parent, ledger)
    origins = [previous, *(Path(path) for path in prior["historical_reservations"]["origins"])]
    reservations = audit_historical_reservations(ledger, origins)
    budget = ledger.budget()
    if budget["blocked"]:
        raise ValueError("the shared ledger is blocked")

    store = copy_closed_seed(previous, output, ledger)
    roots = seed_snapshot(store, options, [seed_task])["root_ids"]
    if len(roots) != 1 or store.active_library() is not None:
        raise ValueError("expected exactly one unpublished root skill")
    with store.connect() as db:
        unfinished = db.execute("SELECT 1 FROM runs WHERE split='test' OR state='RUNNING' LIMIT 1").fetchone()
        seed_row = db.execute("SELECT evidence_hash FROM skills WHERE id=?", (roots[0],)).fetchone()
    if unfinished:
        raise ValueError("the seed state has an unresolved or prior holdout execution")
    evidence = store.get_artifact(seed_row["evidence_hash"])
    evidence_runs = [episode["run_id"] for episode in evidence["episodes"]]
    if {store.run(run)["task_id"] for run in evidence_runs} != {seed_task}:
        raise ValueError("root evidence must come from the predeclared seed only")

    protocol = {
        "schema": 1,
        "claim": "parent_child_diagnostic",
        "origin": str(previous),
        "origin_result_hash": digest(prior_result),
        "manifest": str(manifest),
        "manifest_hash": digest(read(manifest)),
        "verification": str(proof),
        "verification_hash": digest(read(proof)),
        "settings": options.public(),
        "billing_unit": options.accounting_unit,
        "budget_before": budget,
        "historical_reservations": reservations,
        "seed_state_hash": state_snapshot(store),
        "parent": roots[0],
        "seed_evidence_runs": evidence_runs,
        "follow_up_tasks": follow_up,
        "dev_tasks": dev,
        "workers": 1,
        "feedback_policy": "learn_outcome",
        "max_proposals": 1,
        "test_tasks": [task.id for task in holdouts],
        "holdout_repeats": 2 if holdouts else 0,
        "holdout_freshness_databases": freshness_databases,
        "publication": False,
    }
    return freeze_protocol(output, protocol)


def summarize_feedback(store, evolution, run, parent):
    outcome = run["result"]
    trial = {
        "task_id": run["task_id"], "run_id": run["id"], "state": run["state"], "passed": outcome["passed"],
        "route": outcome["routing"]["decision"], "route_reason": outcome["routing"].get("reason"),
        "applied_skill": run["skill_id"], "eligible_for_revision": False,
    }
    if run["state"] in {"COMPLETE", "FAILED", "EXHAUSTED"}:
        feedback = evolution.revision_feedback(run, store.events(run["id"]))
        trial["execution_feedback"] = feedback
        trial["eligible_for_revision"] = run["skill_id"] == parent and evolution.qualifies({
            "public_failed": outcome["public_failed"], "execution_feedback": feedback,
        })
    return trial


def retained_feedback(settings, store, tasks, protocol, previous_result):
    archived = previous_result["follow_up"]
    archived_ids = [trial["run_id"] for trial in archived]
    require_current_model_input(store, [*protocol["seed_evidence_runs"], *archived_ids])
    evolution = Evolution(settings, store, feedback_policy=protocol["feedback_policy"])
    for key, trial in zip(protocol["follow_up_tasks"], archived, strict=True):
        run = store.run(trial["run_id"])
        if (
            run["task_id"] != key or run["task_hash"] != digest(tasks[key].model_dump())
            or run["mode"] != settings.mode or run["split"] != "learn" or run["purpose"] != "solve"
            or run["config"] != protocol["settings"] or run["state"] not in {"COMPLETE", "FAILED", "EXHAUSTED"}
            or summarize_feedback(store, evolution, run, protocol["parent"]) != trial
        ):
            raise ValueError("retained feedback differs from the recorded learning execution")
    if not any(trial["eligible_for_revision"] for trial in archived):
        raise ValueError("retained learning has no qualifying failed parent application")
    return archived


def check_grounding_receipt(receipt: dict, result: dict) -> str:
    learning_runs = [
        trial["run_id"] for trial in result["follow_up"]
        if trial["task_id"] == "pallets-click-29bacc773e" and trial["eligible_for_revision"]
    ]
    if (
        type(receipt.get("schema")) is not int or receipt["schema"] != 1
        or [receipt.get("run_id")] != learning_runs
        or receipt.get("old_candidate") != result["candidate"]
        or receipt.get("parent") != result["protocol"]["parent"]
        or receipt.get("source_result_hash") != digest(result)
        or receipt.get("false_summary_rejected") is not True or receipt.get("canonical_accepted") is not True
        or type(receipt.get("model_calls")) is not int or receipt["model_calls"] != 0
    ):
        raise ValueError("grounded revision requires the verified Click #3509 negative and positive controls")
    return digest(receipt)


def split_retry_dev(catalog, prior, result, old):
    dev = [task for task in catalog if task.split == "dev"]
    earlier = set(prior["dev_tasks"]) | set(prior.get("regression_tasks", []))
    known = [task.id for task in dev if task.id in earlier]
    fresh = [task for task in dev if task.id not in earlier]
    required = set(result["diagnostic"]["regressions"]) | set(prior.get("regression_tasks", []))
    if (
        not required <= set(known)
        or any(digest(task.model_dump()) != old[task.id] for task in dev if task.id in earlier)
        or len({task.source_group for task in fresh}) < 2
    ):
        raise ValueError("preserve prior regressions and introduce at least two fresh dev groups")
    return known, fresh


def prepare_bounded(
    settings: Settings, previous: Path, output: Path, *, manifest: Path, verification: Path,
    proposal_policy: str = "bounded_public_tests", grounding_check: Path | None = None,
) -> dict:
    previous = previous.resolve()
    manifest, verification = manifest.resolve(), verification.resolve()
    prior, result = read(previous / "protocol.json"), read(previous / "result.json")
    grounded = proposal_policy == "grounded_patch_tests"
    if proposal_policy not in {"bounded_public_tests", "grounded_patch_tests"} or grounded != (grounding_check is not None):
        raise ValueError("only a grounded retry requires a Click #3509 grounding check")
    if (
        prior["claim"] != "parent_child_diagnostic"
        or result["protocol"] != prior
        or read(previous / "started.json")["protocol_hash"] != digest(prior)
        or not result["completed"] or result["outcome"] != "paired_diagnostic_completed"
        or result["proposal_attempts"] != 1 or not result["candidate"]
        or result["diagnostic"]["gate"]["passed"] or not result["diagnostic"]["regressions"]
        or result["holdout"] != {"status": "blocked_by_dev_gate", "rows": []}
        or result["published_library"] is not None
        or prior.get("proposal_policy", "standard") != ("bounded_public_tests" if grounded else "standard")
    ):
        raise ValueError("retry requires the preceding policy's completed diagnostic with regressions and unused holdouts")
    grounding = {}
    if grounding_check is not None:
        receipt = read(grounding_check)
        receipt_copy = (output / "grounding-check.json").resolve()
        grounding = {
            "grounding_check": str(receipt_copy),
            "grounding_check_hash": check_grounding_receipt(receipt, result),
        }
    archived = {p.name: p.read_text() for p in (previous / "source").glob("*.py")}
    current = sources()
    solver = ("runtime.py", "sandbox.py", "routing.py", "model.py", "contracts.py")
    if grounded:
        solver += ("context.py", "gate.py", "library.py")
    if digest(archived) != prior["source_hash"] or any(current[name] != archived[name] for name in solver):
        raise ValueError("the bounded revision must reuse the previous solver, actions, routing and adapter")
    options = settings.model_copy(update={"image": prior["settings"]["image"]})
    options.validate_live()
    if options.mode != "live" or options.public() != prior["settings"]:
        raise ValueError("the bounded revision must reuse the previous model, solver budget, and ledger")
    old_manifest, old_proof = Path(prior["manifest"]), Path(prior["verification"])
    if digest(read(old_manifest)) != prior["manifest_hash"] or digest(read(old_proof)) != prior["verification_hash"]:
        raise ValueError("original diagnostic data or verification changed")
    old = task_fingerprints(load_catalog(old_manifest))
    seed_task = read(old_manifest)["seed_task_id"]
    catalog = validate_cohort(
        manifest, verification, seed_task, old[seed_task], options.image,
        max_dev_tasks=4 + len(prior.get("regression_tasks", [])),
    )
    tasks = {task.id: task for task in catalog}
    fingerprints = task_fingerprints(catalog)
    learn = [task.id for task in catalog if task.split == "learn"]
    holdouts = [task for task in catalog if task.split == "test"]
    if (
        set(learn) != {seed_task, *prior["follow_up_tasks"]}
        or [task.id for task in holdouts] != prior["test_tasks"]
        or any(fingerprints[key] != old[key] for key in [*learn, *prior["test_tasks"]])
    ):
        raise ValueError("bounded revision must retain the exact learn and holdout snapshots")
    known, fresh = split_retry_dev(catalog, prior, result, old)
    ledger = Store(options.state_dir, unit=options.accounting_unit)
    fresh_databases = check_fresh_holdouts(fresh, previous.parent, ledger, label="dev task")
    holdout_databases = check_fresh_holdouts(holdouts, previous.parent, ledger)
    reservations = audit_historical_reservations(
        ledger, [Path(path) for path in prior["historical_reservations"]["origins"]],
    )
    budget = ledger.budget()
    if budget["blocked"]:
        raise ValueError("the shared ledger is blocked")
    store = copy_closed_seed(previous, output, ledger)
    with store.connect() as db:
        unfinished = db.execute("SELECT 1 FROM runs WHERE split='test' OR state='RUNNING' LIMIT 1").fetchone()
    if unfinished or store.active_library() is not None:
        raise ValueError("archived diagnostic state contains an active library, a held-out or running task")
    library = SkillLibrary(options, store)
    if library.provenance(result["candidate"])["root"] != prior["parent"]:
        raise ValueError("previous candidate is outside the parent family")
    retained_feedback(options, store, tasks, prior, result)
    if grounding_check is not None:
        dump(receipt_copy, receipt)
    return freeze_protocol(output, {
        **prior,
        "origin": str(previous),
        "origin_result_hash": digest(result),
        "manifest": str(manifest),
        "manifest_hash": digest(read(manifest)),
        "verification": str(verification),
        "verification_hash": digest(read(verification)),
        "budget_before": budget,
        "historical_reservations": reservations,
        "seed_state_hash": state_snapshot(store),
        "proposal_policy": proposal_policy,
        **grounding,
        "rejected_predecessor": result["candidate"],
        "dev_tasks": [task.id for task in fresh],
        "regression_tasks": known,
        "new_dev_freshness_databases": fresh_databases,
        "holdout_freshness_databases": holdout_databases,
    })


def collect_feedback(options, store, tasks, protocol, output, result):
    parent = protocol["parent"]
    runner = Runner(options, store)
    evolution = Evolution(options, store, feedback_policy=protocol["feedback_policy"])

    for key in protocol["follow_up_tasks"]:
        run_id = runner.solve(tasks[key], use_active=False, candidate=True, skill_ids=[parent])
        run = store.run(run_id)
        trial = summarize_feedback(store, evolution, run, parent)
        result["follow_up"].append(trial)
        dump(output / "progress.json", result)
        if run["state"] in {"ERROR", "INTERRUPTED"}:
            raise RuntimeError("follow-up execution failed: " + str(run["result"].get("error")))
    return propose_and_compare(options, store, tasks, protocol, output, result)


def propose_and_compare(options, store, tasks, protocol, output, result):
    parent = protocol["parent"]
    runner = Runner(options, store)
    policy = protocol.get("proposal_policy", "standard")
    grounding = {}
    if policy == "grounded_patch_tests":
        grounding["grounding_tasks"] = {t.id: t for t in tasks.values() if t.split == "learn"}
    evolution = Evolution(options, store, feedback_policy=protocol["feedback_policy"], proposal_policy=policy, **grounding)
    library = SkillLibrary(options, store, runner)
    eligible = [trial["run_id"] for trial in result["follow_up"] if trial["eligible_for_revision"]]
    if not eligible:
        return "no_qualifying_parent_failure"

    require_current_model_input(store, eligible)
    result["phase"] = "propose_revision"
    result["proposal_attempts"] = 1
    dump(output / "progress.json", result)
    try:
        child = evolution.propose([*protocol["seed_evidence_runs"], *eligible], parent)
    except ProposalRejected as exc:
        result["proposal_rejection"] = str(exc)
        return "proposal_rejected"

    if child == protocol.get("rejected_predecessor"):
        result["proposal_rejection"] = "proposal regenerated the rejected artifact"
        return "proposal_rejected"
    result["candidate"] = child
    result["phase"] = "compare_parent_and_child"
    dump(output / "progress.json", result)
    required = library.provenance(child)["support"]

    def record_pair(row, _index):
        result.setdefault("paired_rows", []).append(row)
        dump(output / "progress.json", result)

    result["diagnostic"] = library.evaluate(
        child, [tasks[key] for key in protocol["dev_tasks"]],
        [task for task in tasks.values() if task.id in required],
        diagnostic_parent=parent,
        regression=[tasks[key] for key in protocol.get("regression_tasks", [])],
        on_pair=record_pair,
    )
    return "paired_diagnostic_completed"


def evaluate_holdouts(options, store, tasks, protocol, output, result):
    holdout = result["holdout"]
    if not protocol["test_tasks"]:
        return
    child = result["candidate"]
    if child is None:
        holdout["status"] = "blocked_no_candidate"
        return
    if not result["diagnostic"]["gate"]["passed"]:
        holdout["status"] = "blocked_by_dev_gate"
        return

    parent = protocol["parent"]
    library = SkillLibrary(options, store, Runner(options, store))
    if (
        result["diagnostic"]["baseline_members"] != [parent]
        or result["diagnostic"]["members"] != [child]
        or library.provenance(child)["root"] != parent
    ):
        raise ValueError("holdout needs the same verified parent and child as the paired dev gate")
    test = [tasks[key] for key in protocol["test_tasks"]]
    if any(task.split != "test" for task in test):
        raise ValueError("holdout can only run frozen test tasks")
    check_fresh_holdouts(test, Path(protocol["origin"]).parent, store.ledger or store)
    knowledge = frozen_knowledge(store)
    experiment = digest(protocol)
    stop = Event()
    result["phase"] = "holdout"
    holdout["status"] = "running"
    dump(output / "progress.json", result)
    for repeat in range(protocol["holdout_repeats"]):
        for index, task in enumerate(test):
            store.claim_test(task.id, digest(task.model_dump()), experiment)
            row = library.compare_pair(
                task, repeat, [parent], [child], child, reverse=(index + repeat) % 2 == 1, stop=stop,
            )
            holdout["rows"].append(row)
            dump(output / "progress.json", result)
            if row["infrastructure_error"] or frozen_knowledge(store) != knowledge:
                raise RuntimeError("holdout image mismatch or knowledge changed during evaluation")
    rows = holdout["rows"]
    holdout.update(
        status="complete",
        parent_passes=sum(row["baseline_pass"] for row in rows),
        child_passes=sum(row["candidate_pass"] for row in rows),
        candidate_applications=sum(row["candidate_applied_skill"] == child for row in rows),
        wins=sum(row["candidate_pass"] and not row["baseline_pass"] for row in rows),
        regressions=sum(row["baseline_pass"] and not row["candidate_pass"] for row in rows),
        costs={
            arm: store.costs([row[f"{arm}_run"] for row in rows]) for arm in ("baseline", "candidate")
        },
    )


def execute(settings: Settings, output: Path) -> dict:
    protocol = read(output / "protocol.json")
    options = settings.model_copy(update={"image": protocol["settings"]["image"]})
    options.validate_live()
    if (
        options.public() != protocol["settings"]
        or digest(sources()) != protocol["source_hash"]
    ):
        raise ValueError("diagnostic code or settings changed after preparation")
    manifest = Path(protocol["manifest"])
    prior_result = read(Path(protocol["origin"]) / "result.json")
    if (
        digest(read(manifest)) != protocol["manifest_hash"]
        or digest(read(Path(protocol["verification"]))) != protocol["verification_hash"]
        or digest(prior_result) != protocol["origin_result_hash"]
    ):
        raise ValueError("the frozen input or its provenance changed")
    grounded = protocol.get("proposal_policy") == "grounded_patch_tests"
    if grounded:
        receipt = read(output / "grounding-check.json")
        if check_grounding_receipt(receipt, prior_result) != protocol["grounding_check_hash"]:
            raise ValueError("the frozen grounding check changed")
    reuse_learning = grounded or protocol.get("proposal_policy") == "bounded_public_tests"
    holdout_ids = set(protocol["test_tasks"])
    tasks = {
        task.id: task for task in load_catalog(manifest)
        if task.split in {"learn", "dev"} or task.id in holdout_ids
    }
    ledger = Store(options.state_dir, unit=options.accounting_unit)
    with (ledger.root / "comparison.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("another comparison is using the ledger") from None
        if (output / "started.json").exists():
            raise ValueError("this diagnostic already started")
        reservations = protocol["historical_reservations"]
        if (
            ledger.budget() != protocol["budget_before"]
            or audit_historical_reservations(ledger, [Path(p) for p in reservations["origins"]]) != reservations
        ):
            raise ValueError("ledger or reservation evidence changed")
        check_fresh_holdouts(
            [tasks[key] for key in protocol["test_tasks"]], Path(protocol["origin"]).parent, ledger,
        )
        if reuse_learning:
            check_fresh_holdouts(
                [tasks[key] for key in protocol["dev_tasks"]], Path(protocol["origin"]).parent, ledger, label="dev task",
            )
        store = Store(output / "seed", ledger=ledger)
        if state_snapshot(store) != protocol["seed_state_hash"]:
            raise ValueError("the isolated seed state changed")
        dump(output / "started.json", {"protocol_hash": digest(protocol)})
        result = {
            "protocol": protocol, "phase": "follow_up", "completed": False, "follow_up": [],
            "proposal_attempts": 0, "candidate": None, "diagnostic": None,
            "holdout": {"status": "pending_gate" if holdout_ids else "not_scheduled", "rows": []},
        }
        try:
            if reuse_learning:
                result["follow_up"] = retained_feedback(
                    options, store, tasks, protocol, prior_result,
                )
                result["reused_learning"] = True
                result["outcome"] = propose_and_compare(options, store, tasks, protocol, output, result)
            else:
                result["outcome"] = collect_feedback(options, store, tasks, protocol, output, result)
            evaluate_holdouts(options, store, tasks, protocol, output, result)
            result["phase"] = "complete"
            result["completed"] = True
        except (KeyboardInterrupt, SystemExit):
            result["error"] = "interrupted"
            raise
        except Exception as exc:
            result["error"] = f"{type(exc).__name__}: {str(exc)[:1500]}".replace(options.api_key, "[redacted]")
        finally:
            if result["holdout"]["status"] == "running":
                result["holdout"]["status"] = "stopped"
            elif result["holdout"]["status"] == "pending_gate":
                result["holdout"]["status"] = "blocked_before_gate"
            budget = ledger.budget()
            spent = "spent_" + ledger.unit
            result["budget_after"] = budget
            result["new_cost_" + ledger.unit] = round(budget[spent] - protocol["budget_before"][spent], 12)
            result["published_library"] = store.active_library()
            dump(output / "result.json", result)
        return result
