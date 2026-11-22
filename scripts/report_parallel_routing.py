"""Verify the published three-way decisions offline or reproduce them against the local paid archive."""

import argparse
from collections import Counter
from decimal import Decimal
from pathlib import Path

from evocoder.comparison import dump
from evocoder.contracts import Settings, digest
from evocoder.gate import gate_summary, skill_behavior
from evocoder.jobs import bounded_map
from evocoder.optional_benchmark import SCENARIOS, instance
from evocoder.routing import library_task_view
from falsy_object_cases import build as build_object
from falsy_transfer_cases import make_case
from falsy_transfer_protocol import FAMILIES, probe_patch
from object_revision_cases import cases as object_learning
from portfolio_routing import CHILD, PARENT, POOLS, PRIOR, PUBLIC, ROOT, SEED, VIEWS, cases, selection
from report_click_procedure import Archive, read
from run_falsy_transfer import require
from run_object_revision import order
from run_optional_control import curve_point
from run_parallel_routing import frozen_routes

OUTPUT = Path("docs/results/parallel-routing.json")
HASHES = {PARENT: "parent", CHILD: "child", None: "omitted"}


def domain_tasks(p):
    tasks = {t.id: t for _, (t, _) in cases()}
    for tid in p["objects"]:
        t, _ = build_object(tid.removeprefix("object-"))
        tasks[t.id] = t
    for tid in p["config"]:
        t, _ = make_case(tid.removeprefix("transfer-"), Path("."))
        tasks[t.id] = t
    for t, _ in object_learning():
        if t.id in p["support"]:
            tasks[t.id] = t
    for scenario in SCENARIOS:
        if scenario.split == "learn":
            for i in range(len(scenario.fields)):
                t, _, _ = instance(scenario, i)
                if t.id in p["support"]:
                    tasks[t.id] = t
    return tasks


def summarise(d, p):
    targets = {x["id"]: x["expected"] for x in p["learn"]}
    diagnostic = {}
    for label in ("parent", "child", "skip"):
        diagnostic[label] = {}
        for pool in POOLS:
            diagnostic[label][pool] = {}
            for v in VIEWS:
                rows = [
                    r for r in d["routes"] if (targets[r["task"]], r["pool"], r["view"]) == (label, pool, v)
                ]
                counts = Counter(HASHES[r["selected"]] if r["selected"] else r["decision"] for r in rows)
                diagnostic[label][pool][v] = dict(sorted(counts.items()))
    panels = {}
    for key in ("objects", "config", "support"):
        trials = [r for r in d["dev"] + d["support"] if r["task"] in p[key]]
        panels[key] = {}
        for arm in trials[0]["order"]:
            obs = [r[arm] for r in trials]
            counts = Counter(HASHES[a["selected"]] for a in obs)
            panels[key][arm] = {
                "trials": len(obs),
                "passed": sum(a["passed"] for a in obs),
                "routes": dict(sorted(counts.items())),
                "passed_by_round": [
                    sum(r[arm]["passed"] for r in trials if r["repeat"] == n)
                    for n in sorted({r["repeat"] for r in trials})
                ],
            }
        pairs = []
        for r in trials:
            b, c = r["parent"], r["portfolio"]
            if b["passed"] != c["passed"]:
                pairs.append(
                    {
                        "task": r["task"],
                        "round": r["repeat"] + 1,
                        "change": "win" if c["passed"] else "loss",
                        "before": HASHES[b["selected"]],
                        "after": HASHES[c["selected"]],
                        "same_first_solver_request": b["first_solver_request"] == c["first_solver_request"],
                    }
                )
        panels[key]["paired_changes"] = pairs
    return {"diagnostic": diagnostic, "panels": panels}


def historical(p):
    old = read(PRIOR)
    require(digest(old) == p["prior"], "changed source study")
    rows = []
    for r in old["dev"]:
        if r["task"] in [*p["config"], "object-index-error"]:
            arm = r["child"]
            note = old["route_notes"][arm["run"]]
            rows.append(
                {
                    "task": r["task"],
                    "repeat": r["repeat"],
                    "run": arm["run"],
                    "route": note["decision"],
                    "reason": note["reason"],
                }
            )
    return rows


def checked_quotes(task, view, quotes):
    v = library_task_view(task, view)
    texts = [task.issue, *(x["content"] for x in v.get("public_excerpts", []))]
    return all(q.strip() and any(q in t for t in texts) for q in quotes)


def verify(d, p):
    require(
        d["protocol"] == digest(p)
        and p["parent"] == PARENT
        and p["child"] == CHILD
        and d["historical"] == historical(p),
        "different registration or previous run",
    )
    historic_invalid = [r for r in d["historical"] if r["route"] == "skip_invalid"]
    require(
        len(historic_invalid) == 1
        and d["historic_invalid"]["run"] == historic_invalid[0]["run"]
        and d["historic_invalid"]["validation"] == [{"field": ["task_quotes"], "type": "missing"}],
        "historical transport and relevance decisions conflated",
    )
    tasks = domain_tasks(p)
    targets = {x["id"]: x["expected"] for x in p["learn"]}
    populations = {x["id"]: x["snapshot"] for x in p["learn"]}
    ordered = [
        (tid, n, k, v)
        for n in range(2)
        for i, tid in enumerate(targets)
        for k, v in order(tuple((k, v) for k in POOLS for v in VIEWS), i, n)
    ]
    require(
        [(r["task"], r["repeat"], r["pool"], r["view"]) for r in d["routes"]] == ordered,
        "missing paired diagnoses",
    )
    req = {}
    seen = set()
    for r in d["routes"]:
        task = tasks[r["task"]]
        identity = {
            "protocol": digest(p),
            "task": task.id,
            "snapshot": digest(task.model_dump()),
            "repeat": r["repeat"],
            "pool": r["pool"],
            "view": r["view"],
        }
        require(
            all(r[k] == v for k, v in identity.items())
            and r["identity"] == identity
            and r["run"] == digest(identity)[:32]
            and r["run"] not in seen
            and task.split == "learn"
            and populations[task.id] == identity["snapshot"]
            and digest(library_task_view(task, r["view"])) == r["task_view"]
            and r["decision"] in {"apply", "skip", "uncertain", "skip_invalid"}
            and (r["decision"] == "apply") == bool(r["selected"])
            and r["selected"] in [None, *POOLS[r["pool"]]]
            and checked_quotes(task, r["view"], r["quotes"])
            and (not r["selected"] or (r["quotes"] and r["conditions"])),
            "unbound diagnosis",
        )
        key = (task.id, r["pool"], r["view"])
        require(
            req.setdefault(key, r["request"]) == r["request"], "changed diagnostic request across repeats"
        )
        seen.add(r["run"])
    f = dict(
        selection(d["routes"], targets), protocol=digest(p), decisions=digest(d["routes"]), count=len(ordered)
    )
    require(
        f == d["freeze"]
        and d["view"] == f["view"]
        and d["evaluation"]["choice"] == digest(f)
        and d["evaluation"]["protocol"] == digest(p),
        "different learn-only routing decision",
    )
    dev_order = [
        (t, n, i) for n in range(2) for group in (p["objects"], p["config"]) for i, t in enumerate(group)
    ]
    support_order = [(t, 0, i) for i, t in enumerate(sorted(p["support"]))]
    prior_tasks = {x["id"]: x for x in read(Path("benchmarks/falsy_transfer/object-witness.json"))["tasks"]}
    derived = {"bare": [], "parent": []}
    for label, expected in (("dev", dev_order), ("support", support_order)):
        require(
            [(r["task"], r["repeat"]) for r in d[label]] == [(t, n) for t, n, _ in expected],
            "incomplete repair panel",
        )
        for (tid, n, i), r in zip(expected, d[label], strict=True):
            t = tasks[tid]
            arms = ("bare", "parent", "portfolio") + (
                ("matched_parent",) if label == "dev" and f["view"] != "issue" else ()
            )
            require(
                r["order"] == order(arms, i, n)
                and r["split"] == t.split
                and r["snapshot"] == digest(t.model_dump()) == prior_tasks[tid]["hash"]
                and r["group"] == t.source_group
                and (label != "support" or p["support"][tid] == r["snapshot"]),
                "different frozen repair input",
            )
            for arm in arms:
                a = r[arm]
                pool = POOLS["portfolio"] if arm == "portfolio" else ([] if arm == "bare" else [PARENT])
                view = f["view"] if arm in {"portfolio", "matched_parent"} else "issue"
                require(
                    a["pool"] == pool
                    and a["view"] == view
                    and a["selected"] in [None, *pool]
                    and a["route"]
                    in {"none", "apply", "skip", "skip_invalid", "uncertain", "skip_memory_budget"}
                    and (a["route"] == "apply") == bool(a["selected"])
                    and bool(pool) == bool(a["router_request"])
                    and (bool(pool) or a["route"] == "none")
                    and a["image"] == p["image"]
                    and 1 <= a["solver_calls"] <= p["solver_slots"]
                    and a["calls"] == a["solver_calls"] + bool(pool)
                    and a["run"] not in seen
                    and checked_quotes(t, view, a["quotes"])
                    and (a["state"] == "COMPLETE") == a["passed"]
                    and (not a["passed"] or a["patch"])
                    and (a["probes"] is None or (all(a["probes"].values()) == a["passed"]))
                    and (
                        not a["patch"]
                        or not any(k.startswith("probe_") for k in t.grader_files)
                        or a["probes"]
                    ),
                    "unbound solve/route/grade",
                )
                seen.add(a["run"])
            for base in derived:
                b, c = r[base], r["portfolio"]
                row = {
                    "task_id": tid,
                    "source_group": t.source_group,
                    "split": t.split,
                    "repeat": n,
                    "task_hash": r["snapshot"],
                    "baseline_run": b["run"],
                    "candidate_run": c["run"],
                    "baseline_pass": b["passed"],
                    "candidate_pass": c["passed"],
                    "baseline_applied_skill": b["selected"],
                    "candidate_applied_skill": c["selected"],
                    "baseline_route": b["route"],
                    "candidate_route": c["route"],
                    "infrastructure_error": False,
                }
                row["skill_behavior"] = skill_behavior(row, CHILD, set())
                derived[base].append(row)
    g = d["gate"]
    decisions = {
        "object_parent": gate_summary([r for r in derived["parent"] if r["task_id"] in p["objects"]], 2),
        "whole_parent": gate_summary(derived["parent"], 2),
        "whole_deployed": gate_summary(derived["bare"], 2),
    }
    passed = all(x["gate"]["passed"] for x in decisions.values())
    manifest = {
        "schema": 2,
        "kind": "skill_library",
        "mode": "live",
        "members": POOLS["portfolio"],
        "route_context": f["view"],
    }
    require(
        g["parent_rows"] == derived["parent"]
        and g["rows"] == derived["bare"]
        and g["decisions"] == decisions
        and g["gate"] == {"policy": "parallel_object_config", "passed": passed}
        and g["members"] == POOLS["portfolio"]
        and g["baseline_members"] == []
        and g["candidate"] == CHILD
        and g["library_id"] == digest(manifest)
        and g["protocol"] == digest(p)
        and g["route_freeze"] == digest(f)
        and g["receipt"] == digest({k: v for k, v in g.items() if k != "receipt"}),
        "unbound composite gate",
    )
    require(
        d["published"] == passed
        and d["phase"] == ("complete" if passed else "gate_rejected")
        and d["holdout_exposures"] == len(d["holdout"]) == (30 if passed else 0),
        "wrong exposure state",
    )
    cumulative, points = [], []
    if passed:
        for family in FAMILIES:
            rows = [r for r in d["holdout"] if r["family"] == family]
            require(
                [r["task_id"] for r in rows] == p["holdout"][family] and len(rows) == 10,
                "wrong frozen stratum",
            )
            for r in rows:
                require(
                    r["repeat"] == 0
                    and r["split"] == "test"
                    and not r["infrastructure_error"]
                    and r["task_hash"] == prior_tasks[r["task_id"]]["hash"]
                    and r["baseline_applied_skill"] is None
                    and r["candidate_applied_skill"] in [None, PARENT, CHILD]
                    and not seen.intersection([r["baseline_run"], r["candidate_run"]]),
                    "reused or changed holdout",
                )
                seen.update([r["baseline_run"], r["candidate_run"]])
            cumulative.extend(rows)
            points.append(curve_point(cumulative, family, rows))
    require(d["curve"] == points and d["summary"] == summarise(d, p), "changed metrics")
    known = seen | {x["failed_run"] for x in d["transport"]}
    bill = d["billing"]
    require(
        set(bill) == known
        and sum(x["calls"] for x in bill.values()) == d["costs"]["calls"]
        and sum(x["actual_nanos"] for x in bill.values()) == Decimal(str(d["costs"]["cost_credits"])) * 10**9
        and sum(x["reserved_nanos"] for x in bill.values())
        == Decimal(str(d["costs"]["reserved_credits"])) * 10**9
        and all(bill[r["run"]]["calls"] == 1 for r in d["routes"])
        and all(
            bill[r[a]["run"]]["calls"] == r[a]["calls"] for r in d["dev"] + d["support"] for a in r["order"]
        ),
        "different cost or unallocated attempt",
    )
    return {
        "view": d["view"],
        "object_sources": decisions["object_parent"]["repeat_confirmed_improved_groups"],
        "release": passed,
        "exposed": d["holdout_exposures"],
        "calls": d["costs"]["calls"],
    }


def export(root, seed, ledger_path, p):
    data = read(root / "result.json")
    require(data["completed"], "study still running")
    data["routes"], data["freeze"] = frozen_routes(root, p)
    data["historical"] = historical(p)
    old = Archive(seed / "state/evocoder.sqlite3")
    malformed = next(r for r in data["historical"] if r["route"] == "skip_invalid")
    old_events = old.events(malformed["run"])
    issues = next(
        e["payload"]["issues"]
        for e in old_events
        if e["kind"] == "MODEL_ARGUMENTS_REJECTED" and e["payload"]["schema"] == "LibraryRoute"
    )
    data["historic_invalid"] = {
        "run": malformed["run"],
        "validation": [{"field": i["loc"], "type": i["type"]} for i in issues],
    }
    archive = Archive(root / "state/evocoder.sqlite3")
    ledger = Archive(ledger_path)
    g = data["gate"]
    require(
        archive.get_artifact(g["receipt"]) == {k: v for k, v in g.items() if k != "receipt"}
        and archive.active_library() == (g["library_id"] if data["published"] else None),
        "different persisted release",
    )
    for r in data["routes"]:
        events = archive.events(r["run"])
        (request,) = [e["payload"] for e in events if e["kind"] == "MODEL_RESPONSE"]
        bound = [e["payload"] for e in events if e["kind"] == "ROUTING_RUNTIME_BINDING"]
        require(
            request["request_body_hash"] == r["request"]
            and request["provider_model"].lower() == p["model"].lower()
            and request["expected_function"] == "select_library_skill",
            "different live diagnostic",
        )
        if r["decision"] != "skip_invalid":
            (binding,) = bound
            wire = archive.get_artifact(binding["content_artifact"])
            require(
                binding["skill_id"] == r["selected"]
                and wire["task_quotes"] == r["quotes"]
                and wire["condition_indices"] == r["conditions"]
                and wire["reason"] == r["reason"],
                "different model decision",
            )
        elif bound:
            # A valid wire selection may still have ungrounded quotes or clause indices.
            require(r["selected"] is None, "invalid routing used a skill")
    tasks = domain_tasks(p)
    jobs = {}
    for row in data["dev"] + data["support"]:
        task = tasks[row["task"]]
        for arm in row["order"]:
            a = row[arm]
            r = archive.run(a["run"])
            e = archive.events(a["run"])
            (route,) = [x["payload"] for x in e if x["kind"] == "SKILL_ROUTE"]
            replies = [x["payload"] for x in e if x["kind"] == "MODEL_RESPONSE"]
            solver = [x for x in replies if x["expected_function"] == "submit_action"]
            routers = [x for x in replies if x["expected_function"] == "select_library_skill"]
            require(
                (r["state"], r["purpose"], r["mode"], r["skill_id"], r["task_hash"])
                == (a["state"], "evaluate", "live", a["selected"], row["snapshot"])
                and r["task_id"] == task.id
                and r["result"]["passed"] == a["passed"]
                and r["result"]["patch"] == a["patch"]
                and route["applied_skill_id"] == a["selected"]
                and route["decision"] == a["route"]
                and route["requested_skill_ids"] == a["pool"]
                and route.get("reason", "") == a["reason"]
                and route.get("task_quotes", []) == a["quotes"]
                and route.get("condition_indices", []) == a["conditions"]
                and r["result"]["image_id"] == a["image"]
                and len(solver) == a["solver_calls"]
                and solver[0]["request_body_hash"] == a["first_solver_request"]
                and len(routers) == bool(a["pool"])
                and (routers[0]["request_body_hash"] if routers else None) == a["router_request"]
                and all(x["provider_model"].lower() == p["model"].lower() for x in replies),
                "repair archive differs",
            )
            if a["patch"]:
                patch = archive.get_artifact(a["patch"])
                if a["probes"] is not None:
                    key = task.id, a["patch"]
                    require(
                        jobs.setdefault(key, (task, patch, a["probes"]))[2] == a["probes"],
                        "conflicting patch probes",
                    )
    cfg = Settings(_env_file=None, image=p["image"])

    def repro(entry):
        task, patch, probes = entry
        require(probe_patch(task, patch, cfg) == probes, "independent submitted patch replay differs")

    bounded_map(repro, list(jobs.values()), 3)
    ids = {r["run"] for r in data["routes"]} | {
        r[a]["run"] for r in data["dev"] + data["support"] for a in r["order"]
    }
    for row in data["holdout"]:
        for arm in ("baseline", "candidate"):
            rid = row[arm + "_run"]
            r = archive.run(rid)
            require(
                r["split"] == "test"
                and r["purpose"] == "evaluate"
                and r["task_hash"] == row["task_hash"]
                and r["result"]["passed"] == row[arm + "_pass"]
                and r["skill_id"] == row[arm + "_applied_skill"],
                "different holdout execution",
            )
            ids.add(rid)
    ids.update(x["failed_run"] for x in data["transport"])
    held = {t for family in p["holdout"].values() for t in family}
    with archive.connect() as db:
        model_test = {r[0] for r in db.execute("SELECT task_id FROM runs WHERE split='test'")} & held
    with ledger.connect() as db:
        opened = {r[0] for r in db.execute("SELECT task_id FROM test_exposures")} & held
        require(opened == model_test == {r["task_id"] for r in data["holdout"]}, "unreported test contact")
        cursor = read(root / "start.json")["cursor"]
        bill = {}
        for rid in sorted(ids):
            calls = db.execute(
                "SELECT rowid,state,actual,reserved FROM calls WHERE run_id=? ORDER BY rowid", (rid,)
            ).fetchall()
            require(calls and calls[0]["rowid"] > cursor, "stale billing record")
            bill[rid] = {
                "calls": len(calls),
                "actual_nanos": sum(x["actual"] or 0 for x in calls),
                "reserved_nanos": sum(x["reserved"] for x in calls if x["state"] == "reserved"),
                "last_row": calls[-1]["rowid"],
            }
        end = max(x["last_row"] for x in bill.values())
        require(
            {
                r[0]
                for r in db.execute(
                    "SELECT DISTINCT run_id FROM calls WHERE rowid>? AND rowid<=?", (cursor, end)
                )
            }
            == ids,
            "unattributed calls between first and last receipt",
        )
    data["billing"] = {rid: {k: v for k, v in x.items() if k != "last_row"} for rid, x in bill.items()}
    data["summary"] = summarise(data, p)
    verify(data, p)
    return data


if __name__ == "__main__":
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("command", choices=["export", "verify", "calibrate"])
    cli.add_argument("--root", type=Path, default=ROOT)
    cli.add_argument("--seed", type=Path, default=SEED)
    cli.add_argument("--ledger", type=Path)
    cli.add_argument("--file", type=Path, default=OUTPUT)
    args = cli.parse_args()
    proto = read(PUBLIC)
    if args.command == "export":
        if not args.ledger:
            cli.error("export requires the original --ledger database path")
        dump(args.file, export(args.root, args.seed, args.ledger, proto))
    elif args.command == "calibrate":
        settings = Settings(_env_file=None, image=proto["image"])
        for (label, (task, ref)), r in zip(cases(), proto["learn"], strict=True):
            require(
                (label, digest(task.model_dump()), digest(ref))
                == (r["expected"], r["snapshot"], r["reference"])
                and probe_patch(task, {}, settings) == r["broken"]
                and probe_patch(task, ref, settings) == r["fixed"],
                "diagnostic calibration differs",
            )
        print("Reproduced the six broken and reference diagnostic workspaces.")
    else:
        print(verify(read(args.file), proto))
