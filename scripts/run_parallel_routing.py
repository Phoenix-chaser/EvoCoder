"""Run the registered normal-route portfolio comparison, then conditionally open its original holdout."""

import argparse
import copy
import threading
from pathlib import Path

from evocoder.comparison import dump, frozen_knowledge
from evocoder.contracts import canonical, digest
from evocoder.gate import gate_summary
from evocoder.jobs import bounded_map
from falsy_transfer_protocol import FAMILIES, probe_patch
from portfolio_routing import CHILD, PARENT, POOLS, ROOT, SEED, VIEWS, config, selection, session
from report_click_procedure import read
from run_falsy_transfer import TransferStudy, atomic_trial, require
from run_object_revision import order
from run_optional_control import StudyLibrary, curve_point


def frozen_routes(root, p):
    targets = {x["id"]: x["expected"] for x in p["learn"]}
    recorded = {r["run"]: r for r in [read(f) for f in (root / "routes").glob("*.json")]}
    rows = []
    for n in range(2):
        for i, tid in enumerate(targets):
            for pool, view in order(tuple((k, v) for k in POOLS for v in VIEWS), i, n):
                identity = {
                    "protocol": digest(p),
                    "task": tid,
                    "snapshot": p["learn"][i]["snapshot"],
                    "repeat": n,
                    "pool": pool,
                    "view": view,
                }
                r = recorded[digest(identity)[:32]]
                require(
                    r["identity"] == identity
                    and r["selected"] in [None, *POOLS[pool]]
                    and ((r["decision"] == "apply") == bool(r["selected"])),
                    "wrong diagnostic choice",
                )
                rows.append(r)
    freeze = dict(selection(rows, targets), protocol=digest(p), decisions=digest(rows), count=len(rows))
    require(len(recorded) == 72 and freeze == read(root / "route-freeze.json"), "routing choice not frozen")
    return rows, freeze


class PortfolioStudy(TransferStudy):
    def solve(self, task, phase, repeat=0, *, pool=None, view=None):
        # An original-parent arm retains its deployed routing inputs. In the matched arm only the pool differs.
        actual_view = view or (self.selected if pool == POOLS["portfolio"] else "issue")
        scoped = copy.copy(self)
        scoped.settings = self.settings.model_copy(update={"library_route_context": actual_view})
        return TransferStudy.solve(scoped, task, phase, repeat, pool=pool)


def read_arm(study, task, repeat, arm):
    pool = {"bare": [], "parent": [PARENT], "matched_parent": [PARENT], "portfolio": POOLS["portfolio"]}[arm]
    view = study.selected if arm in {"matched_parent", "portfolio"} else "issue"
    phase = "holdout" if task.split == "test" else "gate"
    rid = study.solve(task, phase, repeat, pool=pool, view=view)
    run = study.store.run(rid)
    events = study.store.events(rid)
    result = run["result"]
    (start,) = [e["payload"] for e in events if e["kind"] == "START"]
    (route,) = [e["payload"] for e in events if e["kind"] == "SKILL_ROUTE"]
    replies = [e["payload"] for e in events if e["kind"] == "MODEL_RESPONSE"]
    solver = [x for x in replies if x["expected_function"] == "submit_action"]
    routers = [x for x in replies if x["expected_function"] == "select_library_skill"]
    require(
        run["purpose"] == "evaluate"
        and run["split"] == task.split
        and run["mode"] == "live"
        and result["image_id"] == study.settings.image
        and run["task_hash"] == digest(task.model_dump())
        and run["config"].get("library_route_context", "issue") == start.get("route_context", "issue") == view
        and sorted(pool) == start["requested_skill_ids"] == route["requested_skill_ids"]
        and route["decision"] in {"none", "apply", "skip", "uncertain", "skip_invalid", "skip_memory_budget"}
        and "fixed_route" not in start
        and route["applied_skill_id"] == run["skill_id"]
        and ((route["decision"] == "apply") == bool(run["skill_id"]))
        and run["skill_id"] in [None, *pool]
        and len(routers) == int(bool(pool))
        and 1 <= len(solver) <= 15
        and len(replies) == len(routers) + len(solver)
        and all(x["provider_model"].lower() == study.settings.model.lower() for x in replies),
        "unbound normal run",
    )
    grade = None
    if result["patch"] and any(k.startswith("probe_") for k in task.grader_files):
        grade = probe_patch(task, study.store.get_artifact(result["patch"]), study.settings)
        require(all(grade.values()) == result["passed"], "resubmitted patch disagrees with its grade")
    return {
        "run": rid,
        "passed": result["passed"],
        "state": run["state"],
        "patch": result["patch"],
        "probes": grade,
        "route": route["decision"],
        "selected": run["skill_id"],
        "pool": pool,
        "view": view,
        "reason": route.get("reason", ""),
        "quotes": route.get("task_quotes", []),
        "conditions": route.get("condition_indices", []),
        "first_solver_request": solver[0]["request_body_hash"],
        "router_request": routers[0]["request_body_hash"] if routers else None,
        "solver_calls": len(solver),
        "calls": result["cost"]["calls"],
        "image": result["image_id"],
    }


def results_by_panel(study, p):
    dev = [
        (i, t, n, "dev")
        for n in range(2)
        for group in (p["objects"], p["config"])
        for i, t in enumerate(group)
    ]
    support = [(i, t, 0, "support") for i, t in enumerate(sorted(p["support"]))]

    def one(job):
        i, tid, n, stage = job
        task = study.tasks[tid]
        arms = ("bare", "parent", "portfolio")
        if stage == "dev" and study.selected != "issue":
            arms += ("matched_parent",)
        seq = order(arms, i, n)
        row = {
            "task": tid,
            "group": task.source_group,
            "snapshot": digest(task.model_dump()),
            "split": task.split,
            "repeat": n,
            "order": seq,
        }
        for a in seq:
            row[a] = read_arm(study, task, n, a)
        atomic_trial(study.root / "trials" / f"{stage}-{tid}-{n}.json", row)
        print(
            stage, tid, n + 1, [(a, row[a]["passed"], str(row[a]["selected"])[:6]) for a in seq], flush=True
        )
        return row

    return [bounded_map(one, jobs, p["workers"]) for jobs in (dev, support)]


def gate(study, p, dev, support):
    lib = StudyLibrary(study)
    require(
        lib.retain_parent([], CHILD) == POOLS["portfolio"]
        and lib.provenance(CHILD)["support"] == p["support"],
        "missing provenance or replay",
    )
    require(not {x["group"] for x in dev} & {x["group"] for x in support}, "dev source leakage")
    require(study.store.active_library() is None, "deployment changed before evaluation")
    stop = threading.Event()
    comparisons = {}
    for label, baseline in (("parent", [PARENT]), ("bare", [])):
        paired = []
        for i, trial in enumerate(dev + support):
            task = study.tasks[trial["task"]]
            row = lib.compare_pair(
                task, trial["repeat"], baseline, POOLS["portfolio"], CHILD, reverse=bool(i % 2), stop=stop
            )
            require(
                row["baseline_run"] == trial[label]["run"]
                and row["candidate_run"] == trial["portfolio"]["run"],
                "gate replays different attempts",
            )
            paired.append(row)
        comparisons[label] = paired
    original = comparisons["parent"]
    decisions = {
        "object_parent": gate_summary([r for r in original if r["task_id"] in p["objects"]], 2),
        "whole_parent": gate_summary(original, 2),
        "whole_deployed": gate_summary(comparisons["bare"], 2),
    }
    passed = all(g["gate"]["passed"] for g in decisions.values())
    manifest = {
        "schema": 2,
        "kind": "skill_library",
        "mode": "live",
        "members": POOLS["portfolio"],
        "route_context": study.selected,
    }
    library_id = study.store.artifact("skill_library", manifest)
    report = {
        "schema": 3,
        "kind": "library_evaluation",
        "mode": "live",
        "baseline_library": None,
        "baseline_members": [],
        "members": POOLS["portfolio"],
        "candidate": CHILD,
        "library_id": library_id,
        "protocol": study.protocol,
        "route_freeze": digest(read(study.root / "route-freeze.json")),
        "config_hash": digest(study.settings.public()),
        "rows": comparisons["bare"],
        "parent_rows": original,
        "decisions": decisions,
        "gate": {"policy": "parallel_object_config", "passed": passed},
    }
    receipt = study.store.artifact("library_evaluation", report)
    with study.store.connect(write=True) as db:
        db.execute(
            "INSERT OR IGNORE INTO library_evaluations VALUES(?,?,?,?,?)",
            (receipt, library_id, None, canonical(report), int(passed)),
        )
    return {**report, "receipt": receipt}


def evaluate(root, seed, cfg, p, store, ledger):
    complete = root / "result.json"
    if complete.exists():
        result = read(complete)
        require(result["completed"] and result["protocol"] == digest(p), "different final result")
        return result
    routes, freeze = frozen_routes(root, p)
    s = PortfolioStudy(root, cfg, store, ledger, p)
    s.selected = freeze["view"]
    require(
        {t: digest(s.tasks[t].model_dump()) for t in p["support"]} == p["support"],
        "different support snapshots",
    )
    registration = root / "evaluation-freeze.json"
    frozen = {"protocol": digest(p), "choice": digest(freeze), "driver": digest(Path(__file__).read_text())}
    atomic_trial(registration, frozen)
    print("normal solver with frozen portfolio view", s.selected, flush=True)
    dev, support = results_by_panel(s, p)
    gate_path = root / "gate.json"
    if not gate_path.exists():
        atomic_trial(gate_path, gate(s, p, dev, support))
    g = read(gate_path)
    require(
        store.get_artifact(g["receipt"]) == {k: v for k, v in g.items() if k != "receipt"}
        and g["protocol"] == s.protocol
        and g["route_freeze"] == frozen["choice"],
        "unbound release",
    )
    result = {
        "protocol": s.protocol,
        "view": s.selected,
        "evaluation": frozen,
        "dev": dev,
        "support": support,
        "gate": g,
        "holdout": [],
        "curve": [],
        "published": False,
        "phase": "gate_rejected",
    }
    if g["gate"]["passed"]:
        if s.store.active_library() is None:
            s.store.activate_library(g["library_id"], g["receipt"], expected=None, mode="live")
        require(s.store.active_library() == g["library_id"], "different published portfolio")
        result.update(published=True, phase="frozen_holdout")
        snapshot = frozen_knowledge(store)
        lib = StudyLibrary(s)
        stop = threading.Event()
        for family in FAMILIES:

            def pair(item):
                i, tid = item
                task = s.tasks[tid]
                require(
                    task.split == "test" and store.active_library() == g["library_id"],
                    "holdout requires published library",
                )
                ledger.claim_test(tid, digest(task.model_dump()), s.protocol)
                row = lib.compare_pair(task, 0, [], POOLS["portfolio"], CHILD, reverse=bool(i % 2), stop=stop)
                return dict(row, family=family)

            pairs = bounded_map(pair, list(enumerate(p["holdout"][family])), p["workers"])
            result["holdout"].extend(pairs)
            result["curve"].append(curve_point(result["holdout"], family, pairs))
            dump(root / "progress.json", result)
        require(snapshot == frozen_knowledge(store), "learning occurred during final testing")
        result["phase"] = "complete"
    with ledger.connect() as db:
        exposed = {
            r[0]
            for r in db.execute("SELECT task_id FROM test_exposures")
            if r[0] in {t for group in p["holdout"].values() for t in group}
        }
        hits = {
            r[0]
            for r in db.execute(
                "SELECT DISTINCT run_id FROM calls WHERE rowid>?", (read(root / "start.json")["cursor"],)
            )
        }
    require(exposed == {r["task_id"] for r in result["holdout"]}, "unreported shared holdout exposure")
    known = {r["run"] for r in routes} | {f.stem for f in (root / "slots").glob("*.json")}
    require(hits == known, "unattributed costs or missing paid attempts")
    result.update(
        holdout_exposures=len(exposed),
        costs=ledger.costs(sorted(known)),
        completed=True,
        transport=[read(f) for f in sorted((root / "transport").glob("*.json"))],
    )
    require(result["published"] == bool(store.active_library()), "unreported activation")
    dump(complete, result)
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--seed", type=Path, default=SEED)
    parser.add_argument("--env-file", type=Path, default=Path(".env.glm"))
    args = parser.parse_args()
    cfg = config(args.env_file)
    with session(args.root, args.seed, cfg) as (p, ledger, store):
        result = evaluate(args.root, args.seed, cfg, p, store, ledger)
        print(result["phase"], result["costs"], flush=True)
