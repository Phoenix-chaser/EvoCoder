"""Paired interventions on two previously observed Click routing disagreements."""

import fcntl
import json
import shutil
import sqlite3
from contextlib import closing
from pathlib import Path
from threading import Event

from evocoder.comparison import dump, frozen_knowledge, sources, state_snapshot
from evocoder.contracts import Settings, digest
from evocoder.dataset import load_catalog
from evocoder.gate import gate_summary
from evocoder.library import SkillLibrary
from evocoder.reservations import audit_historical_reservations
from evocoder.revision_diagnostic import check_fresh_holdouts, copy_closed_seed, freeze_protocol, read
from evocoder.route_analysis import analyze_routes, write_route_report
from evocoder.store import Store


CASES = ("pallets-click-3d029e03eb", "pallets-click-42e2821ba3")
POLICIES = ("normal", "fixed_apply", "fixed_skip")


def schedule():
    """Rotate policies across tasks; reverse arm order within each repeated cell."""
    cells = []
    for rep in range(2):
        for slot in range(len(POLICIES)):
            for index, task in enumerate(CASES):
                policy_index = (slot + index + rep) % len(POLICIES)
                cells.append({"task_id": task, "repeat": rep, "policy": POLICIES[policy_index],
                              "reverse": bool((policy_index + index + rep) % 2)})
    return cells


def prepare(settings: Settings, previous: Path, audit_path: Path, output: Path):
    previous, output = previous.resolve(), output.resolve()
    prior, result = read(previous / "protocol.json"), read(previous / "result.json")
    if (prior.get("proposal_policy") != "grounded_patch_tests" or result["protocol"] != prior
        or not result["completed"] or not result.get("candidate") or result.get("published_library")
        or result["holdout"] != {"status": "blocked_by_dev_gate", "rows": []}
        or read(previous / "started.json")["protocol_hash"] != digest(prior)):
        raise ValueError("expected the completed grounded diagnostic with unused holdouts")
    options = settings.model_copy(update={"image": prior["settings"]["image"]})
    options.validate_live()
    if options.public() != prior["settings"] or options.mode != "live" or options.provider != "glm":
        raise ValueError("reuse the grounded study's GLM, billing and solver configuration")
    manifest = Path(prior["manifest"])
    if digest(read(manifest)) != prior["manifest_hash"] or digest(read(Path(prior["verification"]))) != prior["verification_hash"]:
        raise ValueError("changed benchmark or verification")
    tasks = {t.id: t for t in load_catalog(manifest)}
    if (not set(CASES) <= set(prior["dev_tasks"]) or len(prior["dev_tasks"]) != 3
        or len({tasks[k].source_group for k in prior["dev_tasks"]}) != 3):
        raise ValueError("gate requires two exposed development groups and one remaining independent group")
    archive = {p.name: p.read_text() for p in (previous / "source").glob("*.py")}
    current = sources()
    if digest(archive) != prior["source_hash"] or any(archive[k] != current[k] for k in
            ("routing.py", "sandbox.py", "contracts.py", "context.py", "gate.py")):
        raise ValueError("frozen routing contract, sandbox, context or gate changed")
    audit = read(audit_path)
    if (audit.get("schema") != 1 or audit.get("source_result_hash") != digest(result)
        or audit.get("frozen_source_hash") != prior["source_hash"] or audit.get("model_calls") != 0
        or audit.get("parent") != prior["parent"] or audit.get("candidate") != result["candidate"]
        or tuple(audit.get("focus_tasks", [])) != CASES):
        raise ValueError("historical route audit is not bound to this study")
    route_bodies = {k: {} for k in CASES}
    for group in audit["groups"]:
        arm = group["arm"]
        if (arm not in {"baseline", "candidate"} or group["task_id"] not in CASES
            or not group["matching_recorded_inputs"] or not group["matching_reconstructed_http_body"]
            or len(group["runs"]) != 2 or len({x["rebuilt_body_hash"] for x in group["runs"]}) != 1
            or group["skill_id"] != (prior["parent"] if arm == "baseline" else result["candidate"])):
            raise ValueError("unverified historical route reconstruction")
        route_bodies[group["task_id"]][arm] = group["runs"][0]["rebuilt_body_hash"]
    if len(audit["groups"]) != 4 or not all(len(arms) == 2 for arms in route_bodies.values()):
        raise ValueError("missing historical routing arm")
    ledger = Store(options.state_dir, unit=options.accounting_unit)
    pending = prior["historical_reservations"]
    if audit_historical_reservations(ledger, [Path(p) for p in pending["origins"]]) != pending or ledger.budget()["blocked"]:
        raise ValueError("unresolved ledger changes")
    n = check_fresh_holdouts([tasks[k] for k in prior["test_tasks"]], previous.parent, ledger)
    store = copy_closed_seed(previous, output, ledger)
    library = SkillLibrary(options, store)
    parent, child = prior["parent"], result["candidate"]
    ancestry = library.provenance(child)
    if (store.get_artifact(child)["parent"] != parent or ancestry["root"] != parent or store.active_library()
        or any(digest(tasks[k].model_dump()) != h for k,h in ancestry["support"].items())):
        raise ValueError("candidate or support evidence changed")
    receipt = read(previous / "diagnosis" / "terminal-check.json")["candidate_grounding"]
    if receipt["candidate"] != child or store.get_artifact(receipt["summary_receipt"])["candidate"] != child:
        raise ValueError("candidate lacks the archived grounded summary")
    dump(output / "historical-routes.json", audit)
    return freeze_protocol(output, {
        "schema": 1, "claim": "routing_intervention_diagnostic", "origin": str(previous),
        "origin_result_hash": digest(result), "audit_hash": digest(audit), "manifest": str(manifest),
        "manifest_hash": prior["manifest_hash"], "settings": options.public(), "parent": parent, "child": child,
        "tasks": {k: tasks[k].source_group for k in CASES}, "repeats": 2, "schedule": schedule(),
        "historical_route_bodies": route_bodies,
        "prior_dev_tasks": prior["dev_tasks"], "regression_tasks": prior["regression_tasks"],
        "support_tasks": sorted(ancestry["support"]), "test_tasks": prior["test_tasks"],
        "holdout_repeats": prior["holdout_repeats"], "holdout_freshness_databases": n,
        "gate_policy": "original full normal dev and support gate; stop when normal regressions or too few remaining groups make passage impossible; interventions are diagnostic",
        "publication": False, "budget_before": ledger.budget(), "historical_reservations": pending,
        "seed_state_hash": state_snapshot(store), "knowledge_hash": frozen_knowledge(store),
    })


def pair(library, task, repeat, policy, reverse, protocol):
    fixed = {"normal": None, "fixed_apply": "apply", "fixed_skip": "skip"}[policy]
    row = library.compare_pair(task, repeat, [protocol["parent"]], [protocol["child"]], protocol["child"],
                               reverse=reverse, stop=Event(), fixed_route=fixed)
    row.update(policy=policy, arm_order=["candidate", "baseline"] if reverse else ["baseline", "candidate"])
    for arm in row["arm_order"]:
        key = row[arm + "_run"]
        run = library.store.run(key)
        res = run["result"]
        route = res["routing"]
        row.update({
            arm + "_route_source": route.get("source", "semantic"),
            arm + "_solver_memory_hash": route.get("solver_memory_hash"),
            arm + "_model_steps": res["model_steps"], arm + "_budget_steps": res.get("budget_steps", res["model_steps"]),
            arm + "_credits": library.store.costs([key])["cost_credits"],
        })
        events = [e["payload"] for e in library.store.events(key) if e["kind"] == "MODEL_RESPONSE"]
        row[arm + "_first_solver_request_hash"] = next((e["request_body_hash"] for e in events
                                                        if e["expected_function"] == "submit_action"), None)
        row[arm + "_router_request_hash"] = next((e["request_body_hash"] for e in events
                                                  if e["expected_function"] == "select_library_skill"), None)
        if res["image_id"] != protocol["settings"]["image"] or run["config"] != protocol["settings"]:
            raise ValueError("paired solver configuration or image changed")
        if bool(row[arm + "_router_request_hash"]) != (fixed is None) or not row[arm + "_first_solver_request_hash"]:
            raise ValueError("missing or unexpected model-call receipt")
        if fixed is None and task.id in protocol["tasks"]:
            if row[arm + "_router_request_hash"] != protocol["historical_route_bodies"][task.id][arm]:
                raise ValueError("new normal router request differs from historical reconstruction")
    if fixed == "skip" and row["baseline_first_solver_request_hash"] != row["candidate_first_solver_request_hash"]:
        raise ValueError("fixed-skip first solver inputs differ")
    if frozen_knowledge(library.store) != protocol["knowledge_hash"]:
        raise ValueError("skill knowledge changed during evaluation")
    return row


def remaining_gates(library, tasks, protocol, result, output):
    normal_rows = [r for r in result["rows"] if r["policy"] == "normal"]
    rest_dev = [k for k in protocol["prior_dev_tasks"] if k not in protocol["tasks"]]
    ids = [*rest_dev, *protocol["regression_tasks"]]
    full = result["full_gate"]
    result["phase"] = "full_normal_gate"
    full.update(status="running", rows=normal_rows.copy())
    for rep, idx, key in ([(rep, idx, k) for rep in range(protocol["repeats"]) for idx, k in enumerate(ids)]
                          + [(0, idx, k) for idx, k in enumerate(protocol["support_tasks"])]):
        row = pair(library, tasks[key], rep, "normal", bool((rep + idx) % 2), protocol)
        if key in protocol["regression_tasks"]:
            row["evaluation_role"] = "known_regression"
        full["rows"].append(row)
        dump(output / "progress.json", result)
    full.update(status="complete", **gate_summary(full["rows"], protocol["repeats"]))
    if not full["gate"]["passed"]:
        result["holdout"]["status"] = "blocked_by_full_gate"
        return
    selected = [tasks[k] for k in protocol["test_tasks"]]
    check_fresh_holdouts(selected, Path(protocol["origin"]).parent, library.store.ledger)
    result["phase"] = "holdout"
    result["holdout"]["status"] = "running"
    for rep in range(protocol["holdout_repeats"]):
        for idx, task in enumerate(selected):
            library.store.claim_test(task.id, digest(task.model_dump()), digest(protocol))
            row = pair(library, task, rep, "normal", bool((rep + idx) % 2), protocol)
            result["holdout"]["rows"].append(row)
            dump(output / "progress.json", result)
    result["holdout"]["status"] = "complete"


def execute(settings: Settings, output: Path):
    protocol = read(output / "protocol.json")
    opts = settings.model_copy(update={"image": protocol["settings"]["image"]})
    manifest = Path(protocol["manifest"])
    origin = Path(protocol["origin"])
    if (opts.public() != protocol["settings"] or digest(sources()) != protocol["source_hash"]
        or digest(read(manifest)) != protocol["manifest_hash"] or digest(read(origin / "result.json")) != protocol["origin_result_hash"]
        or digest(read(output / "historical-routes.json")) != protocol["audit_hash"]):
        raise ValueError("changed frozen route experiment")
    tasks = {t.id: t for t in load_catalog(manifest)}
    ledger = Store(opts.state_dir, unit=opts.accounting_unit)
    with (ledger.root / "comparison.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        store = Store(output / "seed", ledger=ledger)
        if ((output / "started.json").exists() or state_snapshot(store) != protocol["seed_state_hash"]
            or ledger.budget() != protocol["budget_before"]):
            raise ValueError("already started, or initial state changed")
        check_fresh_holdouts([tasks[k] for k in protocol["test_tasks"]], origin.parent, ledger)
        with store.connect() as db:
            last = db.execute("SELECT COALESCE(MAX(rowid),0) FROM runs").fetchone()[0]
        dump(output / "started.json", {"protocol_hash": digest(protocol), "previous_last_row": last})
        library = SkillLibrary(opts, store)
        result = {"protocol": protocol, "rows": [], "completed": False, "phase": "routing_matrix",
                  "holdout": {"status": "pending_gate", "rows": []}, "full_gate": {"status": "pending", "rows": []},
                  "budget_before": protocol["budget_before"]}
        try:
            for cell in protocol["schedule"]:
                result["rows"].append(pair(library, tasks[cell["task_id"]], cell["repeat"], cell["policy"],
                                           cell["reverse"], protocol))
                dump(output / "progress.json", result)
            result["analysis"] = analyze_routes(result["rows"], tasks=protocol["tasks"], parent=protocol["parent"],
                                                child=protocol["child"], repeats=protocol["repeats"])
            if result["analysis"]["gate"]["passed"]:
                remaining_gates(library, tasks, protocol, result, output)
            else:
                result["full_gate"]["status"] = result["holdout"]["status"] = "blocked_by_route_gate"
            result.update(completed=True, phase="complete")
        except Exception as exc:
            result["error"] = (type(exc).__name__ + ": " + str(exc)).replace(opts.api_key, "[redacted]")[:1500]
        finally:
            if not result["completed"] and result["holdout"]["status"] in {"pending_gate", "running"}:
                result["holdout"]["status"] = "stopped" if result["holdout"]["rows"] else "blocked_incomplete"
            with store.connect() as db:
                ids = [r[0] for r in db.execute("SELECT id FROM runs WHERE rowid>? ORDER BY rowid", (last,))]
            result["run_ids"] = ids
            result["run_costs"] = store.costs(ids)
            result["budget_after"] = ledger.budget()
            result["new_cost_credits"] = round(result["budget_after"]["spent_credits"] - protocol["budget_before"]["spent_credits"], 8)
            if abs(result["new_cost_credits"] - result["run_costs"]["cost_credits"]) > 0.000001:
                result.update(completed=False, error="ledger delta differs from this study's calls")
            dump(output / "result.json", result)
    return result


def export(output: Path, destination: Path):
    p, r = read(output / "protocol.json"), read(output / "result.json")
    destination.mkdir(parents=True, exist_ok=True)
    for file in ("protocol.json", "result.json", "historical-routes.json", "started.json"):
        shutil.copy2(output / file, destination / file)
    db_path = (output / "seed" / "evocoder.sqlite3").resolve()
    wal = db_path.with_name(db_path.name + "-wal")
    query = "?mode=ro" if wal.exists() and wal.stat().st_size else "?mode=ro&immutable=1"
    with closing(sqlite3.connect(db_path.as_uri() + query, uri=True)) as db:
        db.row_factory = sqlite3.Row
        for key in r["run_ids"]:
            run = dict(db.execute("SELECT * FROM runs WHERE id=?", (key,)).fetchone())
            run["result"] = json.loads(run["result"]) if run["result"] else None
            run["config"] = json.loads(run["config"])
            events = [dict(e) for e in db.execute("SELECT seq,kind,payload FROM events WHERE run_id=? ORDER BY seq", (key,))]
            for e in events:
                e["payload"] = json.loads(e["payload"])
            dump(destination / "traces" / (key + ".json"), {"run": run, "events": events})
    write_route_report(destination, p, r)
