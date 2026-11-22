"""Recompute the five-round route study, or audit and export its private execution archive."""

import argparse
import ast
import json
import re
from collections import Counter
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import httpx

from evocoder.comparison import dump
from evocoder.contracts import Settings, Skill, digest
from evocoder.jobs import bounded_map
from evocoder.model import Model
from evocoder.optional_benchmark import SCENARIOS, instance
from evocoder.route_shape import public_test_paths
from evocoder.routing import library_task_view, route_evidence_texts, select_library_skill
from evocoder.runtime import grade, verify_public
from falsy_transfer_protocol import FAMILIES, probe_patch
from portfolio_routing import CHILD, PARENT, POOLS
from recover_shape_study import INCIDENT, REPLAY_PHASE
from report_click_procedure import Archive, read
from report_parallel_routing import HASHES, domain_tasks
from run_falsy_transfer import require
from run_optional_control import curve_point
from run_shape_routing import audit_trial_arms, decisions, pair
from shape_learning_cases import cases
from shape_study import (
    DEV_ARMS,
    PREVIOUS,
    PUBLIC,
    RESULT,
    ROOT,
    SUPPORT_ARMS,
    arm_order,
    diagnostic_design,
    diagnostic_summary,
    route_receipts,
)

PROTOCOL = "42f3a3d7a5886f847d15b2b98d694299777c2fd61afff0171b5f62e7cdc5d823"
DRIVER = "368b493d1bcd0ed03400ff6bdcc1815488455c5379735c926caddd296a68c194"
WITNESS = Path("benchmarks/falsy_transfer/object-witness.json")


def tasks_for(p, opened):
    tasks = domain_tasks(p)
    tasks.update((t.id, t) for _, (t, _) in cases())
    if opened:
        for scenario in SCENARIOS:
            if scenario.split == "test":
                for i in range(len(scenario.fields)):
                    t, _, _ = instance(scenario, i)
                    tasks[t.id] = t
    return tasks


def grounded(task, view, quotes):
    evidence = route_evidence_texts(library_task_view(task, view))
    return all(q.strip() and any(q in t for t in evidence) for q in quotes)


def verify_routes(records, p, tasks):
    order = [(m, n, v) for _, m, n, views in diagnostic_design(p) for v in views]
    require(len(records) == len(order) == 48, "missing learning decisions")
    first = {}
    for r, (meta, n, v) in zip(records, order, strict=True):
        t = tasks[meta["id"]]
        intent = dict(
            protocol=PROTOCOL, task=t.id, snapshot=meta["snapshot"], repeat=n, pool="portfolio", view=v
        )
        require(
            t.split == "learn"
            and t.source_group == meta["group"]
            and digest(t.model_dump()) == meta["snapshot"]
            and all(r[k] == value for k, value in intent.items())
            and r["identity"] == intent
            and r["run"] == digest(intent)[:32]
            and r["task_view"] == digest(library_task_view(t, v))
            and r["selected"] in [None, *POOLS["portfolio"]]
            and r["decision"] in {"apply", "skip", "uncertain", "skip_invalid"}
            and bool(r["selected"]) == (r["decision"] == "apply")
            and (not r["selected"] or (r["quotes"] and r["conditions"]))
            and grounded(t, v, r["quotes"])
            and re.fullmatch(r"[0-9a-f]{64}", r["request"])
            and first.setdefault((t.id, v), r["request"]) == r["request"],
            "diagnostic identity, evidence or repeated request differs",
        )
    require(len({r["run"] for r in records}) == 48, "diagnostic run reused")
    return diagnostic_summary(p, records)


def diagnostic_fixture_evidence(records, p, tasks):
    """Descriptive trace audit. Naming a method is not evidence of executing the fixture."""
    result = {}
    for cohort, items in p["learn"].items():
        details = {}
        for m in items:
            if m["expected"] != "child":
                continue
            t = tasks[m["id"]]
            nodes = [n for path in public_test_paths(t) for n in ast.walk(ast.parse(t.files[path]))]
            fixtures = {
                n.name
                for n in nodes
                if isinstance(n, ast.ClassDef)
                and any(isinstance(f, ast.FunctionDef) and f.name in {"__bool__", "__len__"} for f in n.body)
            }
            direct = {n.func.id for n in nodes if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
            choices = [r for r in records if r["view"] == "public_shape" and r["task"] == t.id]
            details[t.id] = {
                "structured_decisions": len(choices),
                "quoting_truthiness_method": sum(
                    any(name in q for name in ("__bool__", "__len__") for q in r["quotes"]) for r in choices
                ),
                "declared_fixture_classes": sorted(fixtures),
                "direct_public_constructors": sorted(fixtures & direct),
            }
        result[cohort] = details
    return result


def plan(p, phase):
    if phase == "holdout_trials":
        return [
            (t, 0, arm_order(("bare", "portfolio"), i, 0))
            for family in FAMILIES
            for i, t in enumerate(p["holdout"][family])
        ]
    dev = phase == "dev"
    ids = p["objects"] + p["config"] if dev else sorted(p["support"])
    return [
        (tid, n, arm_order(DEV_ARMS if dev else SUPPORT_ARMS, i, n))
        for n in range(p["dev_rounds"] if dev else p["support_rounds"])
        for i, tid in enumerate(ids)
    ]


def summarize(d, p):
    panels = {}
    for domain in ("objects", "config", "support"):
        rows = [r for r in d["dev"] + d["support"] if r["task"] in p[domain]]
        panels[domain] = {}
        for arm in SUPPORT_ARMS if domain == "support" else DEV_ARMS:
            a = [r[arm] for r in rows]
            panels[domain][arm] = {
                "trials": len(a),
                "passed": sum(x["passed"] for x in a),
                "routes": dict(Counter(HASHES[x["selected"]] for x in a)),
                "passed_by_round": [
                    sum(r[arm]["passed"] for r in rows if r["repeat"] == n)
                    for n in range(p["support_rounds"] if domain == "support" else p["dev_rounds"])
                ],
            }
    return panels


def object_opportunities(d, p):
    report = {}
    for tid in p["objects"]:
        rows = [r for r in d["dev"] if r["task"] == tid]
        failures = [r for r in rows if not r["parent"]["passed"]]
        chosen = [r for r in failures if r["portfolio"]["selected"] == CHILD]
        credited = [
            r
            for r in chosen
            if r["portfolio"]["passed"]
            and r["portfolio"]["first_solver_request"] != r["parent"]["first_solver_request"]
        ]
        report[tid] = {
            "total": len(rows),
            "parent_failures": len(failures),
            "issue_portfolio_child_selections": sum(r["issue_portfolio"]["selected"] == CHILD for r in rows),
            "shape_portfolio_child_selections": sum(r["portfolio"]["selected"] == CHILD for r in rows),
            "child_selected_on_parent_failures": len(chosen),
            "child_wins": len(credited),
            "minimum_wins": d["gate"]["decisions"]["objects_vs_parent"]["tasks"][tid]["k"],
        }
        require(
            len(credited) == d["gate"]["decisions"]["objects_vs_parent"]["tasks"][tid]["attributed_wins"],
            "source opportunities disagree with credited wins",
        )
    return report


def check_trials(rows, phase, p, tasks, metadata, seen):
    schema = plan(p, phase)
    require(len(rows) == len(schema), "missing or additional registered repair repetitions")
    for r, (tid, n, order) in zip(rows, schema, strict=True):
        t = tasks[tid]
        require(
            r["order"] == order
            and r["task"] == tid
            and r["repeat"] == n
            and r["group"] == t.source_group == metadata[tid]["source_group"]
            and r["split"] == t.split == metadata[tid]["split"]
            and r["snapshot"] == metadata[tid]["hash"] == digest(t.model_dump())
            and (phase != "support" or p["support"][tid] == r["snapshot"]),
            "different repair task, order, source or snapshot",
        )
        for arm in order:
            a = r[arm]
            pool = POOLS["portfolio"] if "portfolio" in arm else ([PARENT] if arm == "parent" else [])
            view = "public_shape" if arm == "portfolio" else "issue"
            require(
                a["view"] == view
                and a["pool"] == pool
                and a["selected"] in [None, *pool]
                and a["route"] in {"none", "apply", "skip", "uncertain", "skip_invalid", "skip_memory_budget"}
                and bool(a["selected"]) == (a["route"] == "apply")
                and (a["route"] == "none") == (not pool)
                and (not a["selected"] or (a["conditions"] and a["quotes"]))
                and grounded(t, view, a["quotes"])
                and a["image"] == p["image"]
                and a["run"] not in seen
                and re.fullmatch(r"[0-9a-f]{32}", a["run"])
                and re.fullmatch(r"[0-9a-f]{64}", a["first_solver_request"])
                and (bool(re.fullmatch(r"[0-9a-f]{64}", a["router_request"] or "")) == bool(pool))
                and type(a["passed"]) is bool
                and a["state"] in {"COMPLETE", "FAILED", "EXHAUSTED"}
                and (a["state"] == "COMPLETE") == a["passed"]
                and (not a["passed"] or a["patch"])
                and (not a["patch"] or re.fullmatch(r"[0-9a-f]{64}", a["patch"]))
                and bool(a["probes"])
                == bool(a["patch"] and any(x.startswith("probe_") for x in t.grader_files))
                and (
                    a["probes"] is None
                    or (
                        all(type(x) is bool for x in a["probes"].values())
                        and all(a["probes"].values()) == a["passed"]
                    )
                )
                and 1 <= a["solver_calls"] <= p["solver_slots"]
                and a["calls"] == a["solver_calls"] + bool(pool),
                "unbound route, solver budget or submitted grade",
            )
            seen.add(a["run"])


def interrupted(d):
    return {x["run"]: x for x in d["interruption"]["affected"] if not x["original_grade_available"]}


def original_support(d):
    partial = interrupted(d)
    replays = {}
    totals = {arm: {"planned": 0, "graded": 0, "passed": 0, "ungraded": 0} for arm in SUPPORT_ARMS}
    for r in d["support"]:
        for arm in r["order"]:
            a = r[arm]
            originals = [
                key
                for key, x in partial.items()
                if (r["task"], r["repeat"], a["pool"])
                == (x["intent"]["task"], x["intent"]["repeat"], x["intent"]["pool"])
            ]
            totals[arm]["planned"] += 1
            if originals:
                (key,) = originals
                require(a["run"] != key, "lost response incorrectly scored")
                totals[arm]["ungraded"] += 1
                replays[key] = dict(
                    task=r["task"],
                    repeat=r["repeat"],
                    arm=arm,
                    supplementary_run=a["run"],
                    eligible_for_release=False,
                )
            else:
                totals[arm]["graded"] += 1
                totals[arm]["passed"] += int(a["passed"])
    require(set(replays) == set(partial), "different supplemental population")
    return {"originals": totals, "supplementary": replays}


def audit_interruption(archive, d):
    partial = {}
    for x in d["interruption"]["affected"]:
        rid = x["run"]
        events = archive.events(rid)
        require(digest(events[: x["event_count"]]) == x["events_hash"], "changed pre-interruption trace")
        run = archive.run(rid)
        if x["patch"]:
            require(
                run["result"]["grade_recovered_from_submitted_patch"]
                and run["result"]["patch"] == x["patch"]
                and any(e["kind"] == "GRADE" for e in events[x["event_count"] :]),
                "recovery must grade the original submitted patch",
            )
            continue
        require(
            run["state"] == "INTERRUPTED" and run["result"]["repair_grade"] is None,
            "original outcome was imputed",
        )
        replies = [e["payload"] for e in events if e["kind"] == "MODEL_RESPONSE"]
        require(
            all(v["provider_model"].lower() == d["wire_profile"]["model"].lower() for v in replies),
            "incorrect partial-run model",
        )
        routers = [v for v in replies if v["expected_function"] == "select_library_skill"]
        solvers = [v for v in replies if v["expected_function"] == "submit_action"]
        (route,) = [e["payload"] for e in events if e["kind"] == "SKILL_ROUTE"]
        v = dict(
            task=run["task_id"],
            view=run["config"].get("library_route_context", "issue"),
            pool=x["intent"]["pool"],
            selected=run["skill_id"],
            route=route["decision"],
            conditions=route.get("condition_indices", []),
            quotes=route.get("task_quotes", []),
            reason=route.get("reason", ""),
            router_request=routers[0]["request_body_hash"] if routers else None,
            first_solver_request=solvers[0]["request_body_hash"],
            solver_responses=len(solvers),
            repair_grade=None,
        )
        require(
            len(routers) == bool(v["pool"]) and len(replies) == x["cost"]["calls"] - 1,
            "partial model response count differs",
        )
        if routers:
            audit_selection(archive, events, v, v["route"])
        partial[rid] = v
    return partial


def verify_billing(d, seen):
    bill = d["billing"]
    ungraded = interrupted(d)
    trans = {r["failed_run"]: r for r in d["transport"]}
    require(
        len(trans) == len(d["transport"])
        and not (set(trans) & seen)
        and all(r["attempt"] in (0, 1) and r["phase"] in {"gate", "holdout"} for r in trans.values())
        and set(bill) == seen | set(trans) | set(ungraded)
        and not seen.intersection(ungraded),
        "unallocated or reused model attempt",
    )
    ordinals = sorted(i for v in bill.values() for i in v["study_calls"])
    require(
        ordinals == list(range(1, d["costs"]["calls"] + 1))
        and all(
            v["calls"] == len(v["study_calls"])
            and v["calls"] > 0
            and v["actual_nanos"] >= 0
            and (v["reserved_nanos"] > 0) == (key in ungraded)
            for key, v in bill.items()
        )
        and sum(v["actual_nanos"] for v in bill.values()) == Decimal(str(d["costs"]["cost_credits"])) * 10**9
        and sum(v["reserved_nanos"] for v in bill.values())
        == Decimal(str(d["costs"]["reserved_credits"])) * 10**9,
        "unsettled or inconsistent per-run accounting",
    )
    for x in d["interruption"]["affected"]:
        charge = bill[x["run"]]
        require(
            charge["calls"] == x["cost"]["calls"]
            and max(charge["study_calls"]) <= d["interruption"]["last_call_ordinal"]
            and charge["actual_nanos"] == Decimal(str(x["cost"]["cost_credits"])) * 10**9
            and charge["reserved_nanos"] == Decimal(str(x["cost"]["reserved_credits"])) * 10**9
            and all(i in charge["study_calls"] for i in x["open_call_ordinals"]),
            "original interruption charges changed",
        )
    route_calls = sorted(i for r in d["routes"] for i in bill[r["run"]]["study_calls"])
    require(
        all(bill[r["run"]]["calls"] == 1 for r in d["routes"]) and route_calls == list(range(1, 49)),
        "development preceded learning-only registration",
    )
    end = 48
    for phase in ("dev", "support", "holdout_trials"):
        for n in sorted({r["repeat"] for r in d[phase]}):
            group = [r for r in d[phase] if r["repeat"] == n]
            calls = []
            for r in group:
                for arm in r["order"]:
                    a = r[arm]
                    require(bill[a["run"]]["calls"] == a["calls"], "call count differs from paid slot")
                    calls.extend(bill[a["run"]]["study_calls"])
            require(min(calls) > end, "registered rounds overlapped across phases")
            end = max(calls)


WIRE_FIELDS = (
    "provider",
    "model",
    "temperature",
    "max_output_tokens",
    "reasoning_effort",
    "disable_thinking",
)


def verify_router_bodies(d, tasks):
    """Encode with the runtime's provider adapter; intercept the bytes in memory before any network I/O."""
    profile = d["wire_profile"]
    require(
        set(profile) == set(WIRE_FIELDS)
        and profile["model"].lower() == "glm-5.3"
        and profile["provider"] == "glm",
        "unknown routing wire profile",
    )
    cfg = Settings(
        _env_file=None,
        **profile,
        mode="live",
        base_url="https://offline.invalid/v1",
        api_key="offline",
        billing_mode="token_usd",
        budget_usd=1,
        input_price_per_million=1,
        output_price_per_million=1,
        cached_input_price_per_million=0,
    )
    parent = next(
        x["content"] for x in read(Path("docs/results/optional-control.json"))["skills"] if x["id"] == PARENT
    )
    child = read(Path("docs/results/object-witness.json"))["revision"]["content"]
    require(digest(parent) == PARENT and digest(child) == CHILD, "different frozen skill content")
    skills = {k: Skill.model_validate(v) for k, v in ((PARENT, parent), (CHILD, child))}

    selections = [
        *d["routes"],
        *(r[a] for r in d["dev"] + d["support"] + d["holdout_trials"] for a in r["order"]),
    ]
    for row in selections:
        if row["selected"]:
            require(
                all(
                    type(i) is int and 0 <= i < len(skills[row["selected"]].applicability)
                    for i in row["conditions"]
                ),
                "out-of-range skill applicability clause",
            )

    class Captured(Exception):
        pass

    def intercept(req):
        raise Captured(digest(json.loads(req.content)))

    encoder = Model(
        cfg, SimpleNamespace(unit="usd", reserve=lambda *args: "offline"), httpx.MockTransport(intercept)
    )
    hashes = {}
    requests = [(r["task"], r["view"], POOLS["portfolio"], r["request"]) for r in d["routes"]]
    requests.extend(
        (r["task"], a["view"], a["pool"], a["router_request"])
        for r in d["dev"] + d["support"] + d["holdout_trials"]
        for arm in r["order"]
        if (a := r[arm])["pool"]
    )
    requests.extend(
        (a["task"], a["view"], a["pool"], a["router_request"])
        for a in d["partial_runs"].values()
        if a["pool"]
    )
    for task, view, pool, expected in requests:
        key = (task, view, tuple(pool))
        if key not in hashes:
            try:
                select_library_skill(
                    encoder, tasks[task], {k: skills[k] for k in pool}, "offline", context=view
                )
            except Captured as captured:
                hashes[key] = captured.args[0]
            else:
                raise ValueError("routing adapter did not produce an auditable HTTP body")
        require(
            hashes[key] == expected,
            "actual provider request differs from public inputs and frozen skill scopes",
        )
    return {"distinct_request_bodies": len(set(hashes.values())), "actual_routing_calls": len(requests)}


def verify_slot_intents(d, p):
    options = d["slot_options"]
    require(
        set(options) == set(DEV_ARMS)
        and all(re.fullmatch(r"[0-9a-f]{64}", k) for k in options.values())
        and options["parent"] == options["issue_portfolio"] == p["settings"]
        and len(set(options.values())) == 3,
        "unexpected registered model configurations",
    )
    failures = {r["failed_run"]: r for r in d["transport"]}
    allocated = set()
    bill = d["billing"]
    for r in d["dev"] + d["support"] + d["holdout_trials"]:
        previous = 0
        phase = "holdout" if r["split"] == "test" else "gate"
        for name in r["order"]:
            a = r[name]
            for i in range(3):
                intent = {
                    "protocol": PROTOCOL,
                    "phase": phase if i == 0 else f"{phase}-transport-{i}",
                    "task": r["task"],
                    "task_hash": r["snapshot"],
                    "repeat": r["repeat"],
                    "pool": a["pool"],
                    "oracle": False,
                    "options": options[name],
                }
                key = digest(intent)[:32]
                if key in interrupted(d):
                    original = interrupted(d)[key]
                    require(
                        intent == original["intent"] and min(bill[key]["study_calls"]) > previous,
                        "unbound or reordered missing original",
                    )
                    previous = max(d["interruption"]["last_call_ordinal"], max(bill[key]["study_calls"]))
                    key = digest({**intent, "phase": REPLAY_PHASE})[:32]
                require(
                    key in bill and min(bill[key]["study_calls"]) > previous, "different slot or arm order"
                )
                previous = max(bill[key]["study_calls"])
                if key == a["run"]:
                    break
                require(
                    key in failures
                    and failures[key]
                    == {
                        "failed_run": key,
                        "phase": phase,
                        "task": r["task"],
                        "repeat": r["repeat"],
                        "attempt": i,
                    },
                    "superseded call lacks a recorded transport failure",
                )
                allocated.add(key)
            else:
                raise ValueError("successful solve does not match any authorized attempt")
    require(set(failures) == allocated, "unbound transport failure")


def observed_patches(d):
    by_patch = {}
    for r in d["dev"] + d["support"] + d["holdout_trials"]:
        for arm in r["order"]:
            a = r[arm]
            if a["patch"]:
                key = r["task"], a["patch"]
                require(
                    by_patch.setdefault(key, (a["passed"], a["probes"])) == (a["passed"], a["probes"]),
                    "same submitted patch received incompatible grades",
                )
    return by_patch


def verify(d, p):
    require(
        d["protocol"] == digest(p) == PROTOCOL
        and digest(read(PREVIOUS)) == p["prior"]
        and p["parent"] == PARENT
        and p["child"] == CHILD
        and d["completed"],
        "different study",
    )
    require(
        d["interruption"] == read(INCIDENT)
        and digest(d["interruption"]) == "8422c2a21577e41b5c1f473d75df4c0038cd41957e78c8afacdbf06df109f1bd"
        and d["original_support_complete"] is False
        and not d["published"],
        "missing interruption or unauthorized publication",
    )
    require(
        set(d["partial_runs"]) == set(interrupted(d)) and original_support(d) == d["support_accounting"],
        "different original or supplemental arms",
    )
    for key, x in interrupted(d).items():
        a = d["partial_runs"][key]
        require(
            a["task"] == x["intent"]["task"]
            and a["pool"] == x["intent"]["pool"]
            and a["repair_grade"] is None
            and a["view"] == ("public_shape" if len(a["pool"]) == 2 else "issue")
            and a["solver_responses"] + bool(a["pool"]) + 1 == x["cost"]["calls"],
            "missing or imputed partial run",
        )
    tasks = tasks_for(p, False)
    meta = {r["id"]: r for r in read(WITNESS)["tasks"]}
    freeze = verify_routes(d["routes"], p, tasks)
    require(
        d["freeze"] == freeze
        and d["diagnostic_fixture_evidence"] == diagnostic_fixture_evidence(d["routes"], p, tasks)
        and d["evaluation"] == {"protocol": PROTOCOL, "freeze": digest(freeze), "driver": DRIVER},
        "learn-only view was not frozen before development",
    )
    seen = {r["run"] for r in d["routes"]}
    for phase in ("dev", "support"):
        check_trials(d[phase], phase, p, tasks, meta, seen)
    rows = d["dev"] + d["support"]
    expected = {
        tid: (meta[tid]["split"], meta[tid]["source_group"], meta[tid]["hash"])
        for tid in [*p["objects"], *p["config"], *p["support"]]
    }
    comparisons, release = decisions(p, rows, expected)
    manifest = dict(
        schema=2, kind="skill_library", mode="live", members=POOLS["portfolio"], route_context="public_shape"
    )
    gate = dict(
        schema=5,
        kind="library_evaluation",
        mode="live",
        baseline_library=None,
        baseline_members=[],
        members=POOLS["portfolio"],
        candidate=CHILD,
        library_id=digest(manifest),
        protocol=PROTOCOL,
        route_freeze=digest(freeze),
        observations=digest(rows),
        decisions=comparisons,
        interruption=digest(d["interruption"]),
        gate=dict(
            policy="matched_majority_portfolio_v1",
            passed=False,
            original_support_complete=False,
            ungraded_originals=3,
            descriptive_replays=3,
            would_pass_with_replays=release,
        ),
    )
    require(
        d["gate"] == {**gate, "receipt": digest(gate)}
        and d["published"] == release
        and d["phase"] == ("complete" if release else "gate_rejected")
        and d["holdout_exposures"] == len(d["holdout_trials"]) == (30 if release else 0),
        "publication receipt or holdout exposure differs from registered gate",
    )
    pairs, curve = [], []
    if release:
        tasks = tasks_for(p, True)
        check_trials(d["holdout_trials"], "holdout_trials", p, tasks, meta, seen)
        for family in FAMILIES:
            local = [
                dict(pair(r, "bare"), family=family)
                for r in d["holdout_trials"]
                if r["task"] in p["holdout"][family]
            ]
            pairs.extend(local)
            curve.append(curve_point(pairs, family, local))
    patches = observed_patches(d)
    require(
        d["holdout"] == pairs
        and d["curve"] == curve
        and d["summary"] == summarize(d, p)
        and d["object_opportunities"] == object_opportunities(d, p)
        and d["patch_replay"]
        == {
            "unique_submissions": len(patches),
            "named_boundary_checks": sum(probes is not None for _, probes in patches.values()),
        },
        "different observed grades, repair panels or diversity curve",
    )
    require(d["router_body_audit"] == verify_router_bodies(d, tasks), "wire audit totals differ")
    verify_billing(d, seen)
    verify_slot_intents(d, p)
    return {
        "model": p["model"],
        "scores": freeze["scores"],
        "objects": comparisons["objects_vs_parent"]["repeat_confirmed_improved_groups"],
        "release": release,
        "holdout": d["holdout_exposures"],
        "calls": d["costs"]["calls"],
    }


def audit_selection(archive, events, row, decision):
    bindings = [e["payload"] for e in events if e["kind"] == "ROUTING_RUNTIME_BINDING"]
    if decision in {"skip_invalid", "skip_memory_budget"}:
        require(
            row["selected"] is None
            and (decision == "skip_invalid" or (len(bindings) == 1 and bindings[0]["decision"] == "apply")),
            "rejected selection was injected",
        )
        return
    (bound,) = bindings
    wire = archive.get_artifact(bound["content_artifact"])
    require(
        bound["skill_id"] == row["selected"]
        and bound["decision"] == decision
        and wire["skill_index"] == bound["skill_index"]
        and wire["task_quotes"] == row["quotes"]
        and wire["condition_indices"] == row["conditions"]
        and wire["reason"] == row["reason"],
        "model choice, clauses or evidence differs from the archived response",
    )


def export(root, ledger, p):
    d = read(root / "result.json")
    require(d["completed"] and p == read(root / "protocol.json"), "unfinished or different registration")
    d["routes"], d["evaluation"] = route_receipts(root, p), read(root / "evaluation-freeze.json")
    require(d["freeze"] == read(root / "route-freeze.json"), "different recorded route freeze")
    archive = Archive(root / "state/evocoder.sqlite3")
    cfg = Settings(_env_file=None, image=p["image"], model=p["model"])
    g = d["gate"]
    require(
        archive.get_artifact(g["receipt"]) == {k: v for k, v in g.items() if k != "receipt"}
        and archive.active_library() == (g["library_id"] if d["published"] else None),
        "deployment differs from evaluator receipt",
    )
    for r in d["routes"]:
        events = archive.events(r["run"])
        (resp,) = [e["payload"] for e in events if e["kind"] == "MODEL_RESPONSE"]
        require(
            resp["request_body_hash"] == r["request"]
            and resp["provider_model"].lower() == p["model"].lower()
            and resp["expected_function"] == "select_library_skill",
            "diagnostic wire mismatch",
        )
        rejected = [
            i["loc"]
            for e in events
            if e["kind"] == "MODEL_ARGUMENTS_REJECTED"
            for i in e["payload"]["issues"]
        ]
        require(r["invalid_fields"] == rejected, "diagnostic rejection fields differ")
        audit_selection(archive, events, r, r["decision"])
    rows = d["dev"] + d["support"] + d["holdout_trials"]
    solved = audit_trial_arms(SimpleNamespace(store=archive, settings=cfg), rows)
    ids = solved | {r["run"] for r in d["routes"]} | {r["failed_run"] for r in d["transport"]}
    ids.update(interrupted(d))
    slots = {f.stem: read(f) for f in (root / "slots").glob("*.json")}
    require(
        set(slots) == ids - {r["run"] for r in d["routes"]}
        and all(digest(v)[:32] == k for k, v in slots.items()),
        "unreported paid slot",
    )
    options = {}
    for r in rows:
        for arm in r["order"]:
            a = r[arm]
            slot = slots[a["run"]]
            if a["pool"]:
                audit_selection(archive, archive.events(a["run"]), a, a["route"])
            require(
                options.setdefault(arm, slot["options"]) == slot["options"],
                "model configuration changed across repetitions",
            )
            run = archive.run(a["run"])
            require(
                slot["task"] == r["task"]
                and slot["repeat"] == r["repeat"]
                and slot["pool"] == a["pool"]
                and slot["protocol"] == PROTOCOL
                and slot["task_hash"] == r["snapshot"]
                and not slot["oracle"]
                and slot["options"] == digest(run["config"])
                and run["config"]["max_steps"] == (16 if a["pool"] else 15),
                "wrong model call identity or step allowance",
            )
    d["slot_options"] = options
    first = archive.run(rows[0][rows[0]["order"][0]]["run"])["config"]
    d["wire_profile"] = {k: first[k] for k in WIRE_FIELDS}
    d["partial_runs"] = audit_interruption(archive, d)
    d["support_accounting"] = original_support(d)
    held = {t for family in p["holdout"].values() for t in family}
    exposed = {r["task_id"] for r in d["holdout"]}
    with archive.connect() as db:
        require(
            {r[0] for r in db.execute("SELECT task_id FROM runs WHERE split='test'")} & held == exposed,
            "unreported held-out solve",
        )
        receipt = db.execute("SELECT * FROM library_evaluations WHERE id=?", (g["receipt"],)).fetchone()
        require(
            receipt
            and receipt["library_id"] == g["library_id"]
            and receipt["baseline_id"] is None
            and json.loads(receipt["report"]) == archive.get_artifact(g["receipt"])
            and bool(receipt["passed"]) == d["published"],
            "persisted gate mismatch",
        )
    with Archive(ledger).connect() as db:
        require(
            {r[0] for r in db.execute("SELECT task_id FROM test_exposures")} & held == exposed,
            "holdout was exposed in a different run",
        )
        start = read(root / "start.json")["cursor"]
        bill = {}
        for key in ids:
            charges = db.execute(
                "SELECT rowid,state,actual,reserved FROM calls WHERE run_id=? ORDER BY rowid", (key,)
            ).fetchall()
            require(charges and charges[0]["rowid"] > start, "stale or missing paid call")
            bill[key] = {
                "calls": len(charges),
                "study_calls": [r["rowid"] - start for r in charges],
                "actual_nanos": sum(r["actual"] or 0 for r in charges),
                "reserved_nanos": sum(r["reserved"] for r in charges if r["state"] == "reserved"),
            }
        d["billing"] = dict(sorted(bill.items()))
    tasks = tasks_for(p, d["published"])
    d["diagnostic_fixture_evidence"] = diagnostic_fixture_evidence(d["routes"], p, tasks)
    patches = observed_patches(d)

    def replay(item):
        (tid, key), (passed, probes) = item
        task, patch = tasks[tid], archive.get_artifact(key)
        require(set(patch) == set(task.editable) and digest(patch) == key, "unexpected patch boundary")
        if probes is not None:
            require(probe_patch(task, patch, cfg) == probes, "named patch replay differs")
        else:
            public = verify_public(task, patch, cfg, "public-replay")
            hidden = grade(task, patch, cfg, "contract-replay")
            require(
                public["image_id"] == hidden["image_id"] == cfg.image
                and not public["timeout"]
                and not hidden["timeout"]
                and ((public["exit_code"] == 0 and hidden["passed"]) == passed),
                "independent final grade differs",
            )

    bounded_map(replay, list(patches.items()), 4)
    d["patch_replay"] = {
        "unique_submissions": len(patches),
        "named_boundary_checks": sum(v[1] is not None for v in patches.values()),
    }
    d["summary"] = summarize(d, p)
    d["router_body_audit"] = verify_router_bodies(d, tasks)
    d["object_opportunities"] = object_opportunities(d, p)
    verify(d, p)
    return d


if __name__ == "__main__":
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("command", choices=["verify", "export", "calibrate"])
    cli.add_argument("--root", type=Path, default=ROOT)
    cli.add_argument("--ledger", type=Path)
    cli.add_argument("--file", type=Path, default=RESULT)
    args = cli.parse_args()
    protocol = read(PUBLIC)
    if args.command == "export":
        if args.ledger is None:
            cli.error("export needs the original --ledger database path")
        data = export(args.root, args.ledger, protocol)
        dump(args.file, data)
        print(verify(data, protocol))
    elif args.command == "calibrate":
        cfg = Settings(_env_file=None, image=protocol["image"])
        for (label, (task, fix)), meta in zip(cases(), protocol["learn"]["fresh"], strict=True):
            require(
                (label, digest(task.model_dump()), digest(fix))
                == (meta["expected"], meta["snapshot"], meta["reference"])
                and probe_patch(task, {}, cfg) == meta["broken"]
                and probe_patch(task, fix, cfg) == meta["fixed"],
                "fresh task calibration differs",
            )
        print("Reproduced all six fresh pristine failures and reference repairs.")
    else:
        print(verify(read(args.file), protocol))
