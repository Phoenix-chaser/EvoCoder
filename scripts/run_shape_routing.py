"""Evaluate a frozen public-shape router; publish and open the test set only on an authorized receipt."""

import argparse
import copy
from pathlib import Path

from evocoder.comparison import dump, frozen_knowledge
from evocoder.contracts import canonical, digest
from evocoder.gate import skill_behavior
from evocoder.jobs import bounded_map
from evocoder.repeat_gate import repeated_gate
from falsy_transfer_protocol import FAMILIES
from portfolio_routing import CHILD, PARENT, POOLS, config
from report_click_procedure import read
from run_falsy_transfer import atomic_trial, require
from run_optional_control import StudyLibrary, curve_point
from run_parallel_routing import PortfolioStudy, read_arm
from shape_study import (
    DEV_ARMS,
    ROOT,
    SEED,
    SUPPORT_ARMS,
    arm_order,
    diagnostic_summary,
    route_receipts,
    session,
)


def pair(row, base):
    b, c = row[base], row["portfolio"]
    record = {
        "task_id": row["task"],
        "source_group": row["group"],
        "task_hash": row["snapshot"],
        "split": row["split"],
        "repeat": row["repeat"],
        "infrastructure_error": b["image"] != c["image"],
    }
    for name, a in (("baseline", b), ("candidate", c)):
        record.update(
            {
                name + "_run": a["run"],
                name + "_pass": a["passed"],
                name + "_request": a["first_solver_request"],
                name + "_route": a["route"],
                name + "_applied_skill": a["selected"],
            }
        )
    record["skill_behavior"] = skill_behavior(record, CHILD, set())
    return record


def decisions(p, rows, expected):
    choices = {}
    for name, base, ids in (
        ("objects_vs_parent", "parent", p["objects"]),
        ("whole_vs_parent", "parent", [*p["objects"], *p["config"], *p["support"]]),
        ("whole_vs_bare", "bare", [*p["objects"], *p["config"], *p["support"]]),
        ("same_skills_view_comparison", "issue_portfolio", [*p["objects"], *p["config"]]),
    ):
        selected = {tid: expected[tid] for tid in ids}
        choices[name] = repeated_gate([pair(r, base) for r in rows if r["task"] in selected], selected)
    release = all(
        choices[name]["gate"]["passed"] for name in ("objects_vs_parent", "whole_vs_parent", "whole_vs_bare")
    )
    return choices, release


def read_variant(study, task, n, arm):
    if arm == "issue_portfolio":
        legacy = copy.copy(study)
        legacy.selected = "issue"
        return read_arm(legacy, task, n, "portfolio")
    return read_arm(study, task, n, arm)


def trial(study, p, task, n, i, arms, phase):
    path = study.root / "trials" / f"{phase}-{task.id}-{n}.json"
    seq = arm_order(arms, i, n)
    intent = {
        "task": task.id,
        "snapshot": digest(task.model_dump()),
        "split": task.split,
        "group": task.source_group,
        "repeat": n,
        "order": seq,
    }
    if path.exists():
        r = read(path)
        require(
            all(r[k] == v for k, v in intent.items()) and set(r) == set(intent) | set(seq),
            "changed trial intent",
        )
        return r
    r = dict(intent)
    for a in seq:
        r[a] = read_variant(study, task, n, a)
    atomic_trial(path, r)
    print(phase, task.id, n + 1, [(a, r[a]["passed"], str(r[a]["selected"])[:7]) for a in seq], flush=True)
    return r


def collect(study, p, phase):
    dev = phase == "dev"
    ids = p["objects"] + p["config"] if dev else sorted(p["support"])
    rounds = p["dev_rounds"] if dev else p["support_rounds"]
    arms = DEV_ARMS if dev else SUPPORT_ARMS
    result = []
    for n in range(rounds):

        def one(item):
            i, tid = item
            return trial(study, p, study.tasks[tid], n, i, arms, phase)

        group = bounded_map(one, list(enumerate(ids)), p["workers"])
        result.extend(group)
        print("complete", phase, n + 1, {a: sum(r[a]["passed"] for r in group) for a in arms}, flush=True)
    return result


def audit_trial_arms(study, rows):
    seen = set()
    for r in rows:
        for arm in r["order"]:
            a = r[arm]
            run = study.store.run(a["run"])
            events = study.store.events(a["run"])
            (route,) = [e["payload"] for e in events if e["kind"] == "SKILL_ROUTE"]
            replies = [e["payload"] for e in events if e["kind"] == "MODEL_RESPONSE"]
            solvers = [x for x in replies if x["expected_function"] == "submit_action"]
            routers = [x for x in replies if x["expected_function"] == "select_library_skill"]
            pool = (
                POOLS["portfolio"]
                if arm in {"portfolio", "issue_portfolio"}
                else ([PARENT] if arm == "parent" else [])
            )
            view = "public_shape" if arm == "portfolio" else "issue"
            require(
                a["run"] not in seen
                and run["mode"] == "live"
                and run["purpose"] == "evaluate"
                and run["task_hash"] == r["snapshot"]
                and run["task_id"] == r["task"]
                and run["split"] == r["split"]
                and run["state"] in {"COMPLETE", "FAILED", "EXHAUSTED"}
                and run["state"] == a["state"]
                and run["result"]["passed"] == a["passed"]
                and run["result"]["patch"] == a["patch"]
                and run["result"]["image_id"] == a["image"] == study.settings.image
                and run["config"].get("library_route_context", "issue") == a["view"] == view
                and route["requested_skill_ids"] == a["pool"] == pool
                and run["skill_id"] == a["selected"] == route["applied_skill_id"]
                and route["decision"] == a["route"]
                and route.get("reason", "") == a["reason"]
                and route.get("task_quotes", []) == a["quotes"]
                and route.get("condition_indices", []) == a["conditions"]
                and bool(a["selected"]) == (route["decision"] == "apply")
                and a["selected"] in [None, *pool]
                and len(routers) == int(bool(pool))
                and 1 <= len(solvers) == a["solver_calls"] <= 15
                and solvers[0]["request_body_hash"] == a["first_solver_request"]
                and (routers[0]["request_body_hash"] if routers else None) == a["router_request"]
                and len(replies) == a["calls"] == len(routers) + len(solvers)
                and all(x["provider_model"].lower() == study.settings.model.lower() for x in replies),
                "trial disagrees with actual task, wire requests, routing or graded patch",
            )
            seen.add(a["run"])
    return seen


def billing(study, routes):
    ids = {r["run"] for r in routes} | {f.stem for f in (study.root / "slots").glob("*.json")}
    with study.ledger.connect() as db:
        cursor = read(study.root / "start.json")["cursor"]
        hits = {r[0] for r in db.execute("SELECT DISTINCT run_id FROM calls WHERE rowid>?", (cursor,))}
    require(hits == ids, "unowned or missing study calls")
    costs = study.ledger.costs(sorted(ids))
    require(costs["reserved_credits"] == 0, "unsettled billing prevents a release decision")
    return costs


def issue_gate(study, p, out, freeze):
    lib = StudyLibrary(study)
    require(
        study.store.active_library() is None
        and lib.retain_parent([], CHILD) == POOLS["portfolio"]
        and lib.provenance(CHILD)["support"] == p["support"],
        "changed initial deployment or support",
    )
    rows = out["dev"] + out["support"]
    audit_trial_arms(study, rows)
    expected = {
        tid: (t.split, t.source_group, digest(t.model_dump()))
        for tid, t in study.tasks.items()
        if tid in [*p["objects"], *p["config"], *p["support"]]
    }
    comparisons, release = decisions(p, rows, expected)
    manifest = {
        "schema": 2,
        "kind": "skill_library",
        "mode": "live",
        "members": POOLS["portfolio"],
        "route_context": "public_shape",
    }
    target = study.store.artifact("skill_library", manifest)
    report = {
        "schema": 4,
        "kind": "library_evaluation",
        "mode": "live",
        "baseline_library": None,
        "baseline_members": [],
        "members": POOLS["portfolio"],
        "candidate": CHILD,
        "library_id": target,
        "protocol": study.protocol,
        "route_freeze": digest(freeze),
        "observations": digest(rows),
        "decisions": comparisons,
        "gate": {"policy": "matched_majority_portfolio_v1", "passed": release},
    }
    key = study.store.artifact("library_evaluation", report)
    with study.store.connect(write=True) as db:
        db.execute(
            "INSERT OR IGNORE INTO library_evaluations VALUES(?,?,?,?,?)",
            (key, target, None, canonical(report), int(release)),
        )
    return {**report, "receipt": key}


def run(study, p):
    root = study.root
    if (root / "result.json").is_file():
        d = read(root / "result.json")
        require(d["completed"] and d["protocol"] == digest(p), "wrong final result")
        return d
    routes = route_receipts(root, p)
    freeze = diagnostic_summary(p, routes)
    require(freeze == read(root / "route-freeze.json"), "diagnosis must finish before development")
    atomic_trial(
        root / "evaluation-freeze.json",
        {"protocol": digest(p), "freeze": digest(freeze), "driver": digest(Path(__file__).read_text())},
    )
    study.selected = "public_shape"
    dev = collect(study, p, "dev")
    support = collect(study, p, "support")
    out = {
        "protocol": study.protocol,
        "completed": False,
        "freeze": freeze,
        "dev": dev,
        "support": support,
        "published": False,
        "holdout": [],
        "holdout_trials": [],
        "curve": [],
        "phase": "gate_rejected",
    }
    billing(study, routes)
    path = root / "gate.json"
    if not path.exists():
        atomic_trial(path, issue_gate(study, p, out, freeze))
    gate = out["gate"] = read(path)
    require(
        study.store.get_artifact(gate["receipt"]) == {k: v for k, v in gate.items() if k != "receipt"}
        and gate["observations"] == digest(dev + support)
        and gate["protocol"] == digest(p),
        "invalid terminal gate",
    )
    if gate["gate"]["passed"]:
        if study.store.active_library() is None:
            study.store.activate_library(gate["library_id"], gate["receipt"], expected=None, mode="live")
        require(study.store.active_library() == gate["library_id"], "different active library")
        out.update(published=True, phase="frozen_holdout")
        knowledge = frozen_knowledge(study.store)
        for family in FAMILIES:

            def one(item):
                i, tid = item
                task = study.tasks[tid]
                require(
                    task.split == "test" and study.store.active_library() == gate["library_id"],
                    "unauthorized holdout",
                )
                study.ledger.claim_test(tid, digest(task.model_dump()), study.protocol)
                return trial(study, p, task, 0, i, ("bare", "portfolio"), "holdout")

            trials = bounded_map(one, list(enumerate(p["holdout"][family])), p["workers"])
            audit_trial_arms(study, trials)
            rows = [dict(pair(r, "bare"), family=family) for r in trials]
            out["holdout_trials"].extend(trials)
            out["holdout"].extend(rows)
            out["curve"].append(curve_point(out["holdout"], family, rows))
            dump(root / "progress.json", out)
        require(knowledge == frozen_knowledge(study.store), "knowledge changed during test")
        out["phase"] = "complete"
    held = {t for group in p["holdout"].values() for t in group}
    with study.ledger.connect() as db:
        exposed = {r[0] for r in db.execute("SELECT task_id FROM test_exposures")} & held
    with study.store.connect() as db:
        tested = {r[0] for r in db.execute("SELECT task_id FROM runs WHERE split='test'")} & held
    require(
        tested == exposed == {x["task_id"] for x in out["holdout"]}
        and bool(study.store.active_library()) == out["published"],
        "unreported release or test exposure",
    )
    out.update(
        completed=True,
        holdout_exposures=len(exposed),
        costs=billing(study, routes),
        transport=[read(f) for f in sorted((root / "transport").glob("*.json"))],
    )
    dump(root / "result.json", out)
    return out


if __name__ == "__main__":
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--root", type=Path, default=ROOT)
    cli.add_argument("--seed", type=Path, default=SEED)
    cli.add_argument("--env-file", type=Path, default=Path(".env.glm"))
    a = cli.parse_args()
    cfg = config(a.env_file)
    with session(a.root, a.seed, cfg) as (p, ledger, store):
        result = run(PortfolioStudy(a.root, cfg, store, ledger, p), p)
        print(result["phase"], result["costs"], flush=True)
