"""Recompute the callback release result offline or export receipts from its original SQLite archives."""

import argparse
import json
import re
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import httpx

from callback_policy import calibrate_noise, comparisons, pilot_summary
from callback_protocol import ARMS, PUBLIC, RESULT, ROOT, arm_order, code_hashes
from callback_sources import sources
from close_callback_release import incomplete_receipt
from collect_callback_support import ADDENDUM, scored, ungraded
from evocoder.comparison import dump, frozen_knowledge
from evocoder.contracts import Action, Settings, digest, model_task_view
from evocoder.jobs import bounded_map
from evocoder.model import Model
from evocoder.optional_benchmark import SCENARIOS, instance
from evocoder.runtime import SOLVER_PROMPT, grade, verify_public
from falsy_transfer_protocol import FAMILIES, probe_patch
from portfolio_routing import CHILD, PARENT, POOLS
from report_click_procedure import Archive, read
from report_parallel_routing import domain_tasks
from report_shape_routing import (
    WIRE_FIELDS,
    audit_selection,
    grounded,
    observed_patches,
    verify_router_bodies,
)
from run_callback_release import design
from run_falsy_transfer import require
from run_optional_control import curve_point
from run_shape_routing import audit_trial_arms, pair
from shape_study import PUBLIC as PREVIOUS_PROTOCOL
from shape_study import RESULT as PREVIOUS_RESULT

REGISTERED = "1ec246da6a813d0b1386f966d86d7e4bf96f43db9359e6231151da0894925c28"


def panels(d):
    return [
        ("pilot", d["pilot_trials"]),
        ("gate", d["dev"]),
        ("support", d["support"]),
        ("holdout", d["holdout_trials"]),
    ]


def workspaces(p, opened):
    tasks = domain_tasks(p)
    tasks.update((t.id, t) for stages in sources().values() for t, _ in stages.values())
    if opened:
        for scenario in SCENARIOS:
            if scenario.split == "test":
                for index in range(len(scenario.fields)):
                    task, _, _ = instance(scenario, index)
                    tasks[task.id] = task
        require(
            all(digest(tasks[tid].model_dump()) == h for tid, h in p["holdout_hashes"].items()),
            "different original sealed ensemble",
        )
    return tasks


def archived_code_hashes():
    """Reconstruct the paid study's original runtime after the family-rule upgrade."""
    from evocoder.comparison import sources as runtime_sources

    runtime = runtime_sources()
    src = runtime["library.py"]
    original = (PUBLIC.parent / "archive/library-family-v1.txt").read_text()
    runtime["library.py"] = (
        src[: src.index("    def validate_family_pool(")] + original + src[src.index("    def replace(") :]
    )
    return {**code_hashes(), "runtime": digest(runtime)}


def check_registration(p):
    history = read(PREVIOUS_RESULT)
    previous = read(PREVIOUS_PROTOCOL)
    require(
        digest(p) == REGISTERED
        and p["code"] == archived_code_hashes()
        and p["previous_result"] == digest(history)
        and p["previous_protocol"] == digest(previous)
        and p["noise"] == calibrate_noise(history, p["config"])
        and p["holdout"] == previous["holdout"]
        and not history["holdout_exposures"],
        "different registration, historical null or test assignment",
    )
    for stages in sources().values():
        for stage, (task, ref) in stages.items():
            m = next(f[stage] for f in p["families"].values() if f[stage]["id"] == task.id)
            require(
                m["snapshot"] == digest(task.model_dump()) and m["reference"] == digest(ref),
                "changed workspace or evaluator reference",
            )
    return history


def expected(p, phase):
    if phase == "pilot":
        return [(f["pilot"]["id"], n, ["parent"]) for n in range(5) for f in p["families"].values()]
    out = []
    for i in range({"gate": 7, "support": 3, "holdout": 3}[phase]):
        ids, arms = design(p, phase, i)
        n = 0 if phase == "holdout" else i
        out.extend((t, n, arm_order(t, n, arms)) for t in ids)
    return out


def wire_audit(d, tasks):
    """Reconstruct both the selection request and the first execution request without transmitting either."""
    all_rows = d["pilot_trials"] + d["dev"] + d["support"] + d["holdout_trials"]
    view = dict(
        wire_profile=d["wire_profile"],
        routes=[],
        dev=[
            dict(r, order=[a for a in r["order"] if not r[a]["pool"] or r[a]["router_request"]])
            for r in all_rows
        ],
        support=[],
        holdout_trials=[],
        partial_runs={},
    )
    routing = verify_router_bodies(view, tasks)
    profile = d["wire_profile"]
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

    class Captured(Exception):
        pass

    def capture(req):
        raise Captured(digest(json.loads(req.content)))

    encoder = Model(
        cfg, SimpleNamespace(unit="usd", reserve=lambda *args: "offline"), httpx.MockTransport(capture)
    )
    parent = next(
        x["content"] for x in read(Path("docs/results/optional-control.json"))["skills"] if x["id"] == PARENT
    )
    child = read(Path("docs/results/object-witness.json"))["revision"]["content"]
    skills = {PARENT: parent, CHILD: child}
    cache = {}
    for r in all_rows:
        for a in r["order"]:
            arm = r[a]
            if arm["first_solver_request"] is None and arm["passed"] is None:
                continue
            key = r["task"], arm["selected"]
            if key not in cache:
                payload = dict(
                    task=model_task_view(tasks[key[0]].public()),
                    skills=[skills[key[1]]] if key[1] else [],
                    history=[],
                    historical_experience=[],
                    remaining_steps=15,
                )
                try:
                    encoder.request(Action, SOLVER_PROMPT, payload, "offline")
                except Captured as c:
                    cache[key] = c.args[0]
                else:
                    raise ValueError("solver request was not captured")
            require(
                arm["first_solver_request"] == cache[key],
                "submitted first solver request differs from assigned skill",
            )
    return {**routing, "distinct_first_solver_requests": len(set(cache.values()))}


def schedule_and_billing(d, p, history, tasks):
    seen = set()
    positions = []
    details = d["charges"]
    end = 0
    for phase, rows in panels(d):
        plan = expected(p, phase) if (phase != "holdout" or d["published"]) else []
        require(len(plan) == len(rows), "missing or additional slots")
        if not plan:
            continue
        by_batch = {}
        subtotal = held = 0
        for r, (tid, n, order) in zip(rows, plan, strict=True):
            task = tasks[tid]
            require(
                r["task"] == tid
                and r["repeat"] == n
                and r["order"] == order
                and r["split"] == task.split
                and r["group"] == task.source_group
                and r["snapshot"] == digest(task.model_dump()),
                "substituted repair or run order",
            )
            batch = (
                next(i for i, f in enumerate(FAMILIES) if tid in p["holdout"][f]) if phase == "holdout" else n
            )
            bucket = by_batch.setdefault(batch, [])
            last = 0
            for name in order:
                a = r[name]
                pool = {"bare": [], "parent": [PARENT], "portfolio": POOLS["portfolio"]}[name]
                view = "public_shape" if name == "portfolio" else "issue"
                active = d["gate"]["library_id"] if phase == "holdout" and name == "portfolio" else None
                intent = dict(
                    protocol=d["protocol"],
                    phase="published-holdout" if active else ("gate" if phase == "support" else phase),
                    task=tid,
                    task_hash=r["snapshot"],
                    repeat=n,
                    pool=pool,
                    oracle=False,
                    options=history["slot_options"][name],
                )
                if active:
                    intent["library"] = active
                rid = a["run"]
                require(
                    rid == digest(intent)[:32]
                    and rid not in seen
                    and a["pool"] == pool
                    and a["view"] == view
                    and a["image"] == p["image"]
                    and a["selected"] in [None, *pool]
                    and (not a["selected"] or (a["conditions"] and a["quotes"]))
                    and grounded(task, view, a["quotes"])
                    and (pool or a["router_request"] is None),
                    "unbound run, route or source",
                )
                if a["passed"] is None:
                    e = a["incident"]
                    require(
                        phase == "support"
                        and a["state"] == "ERROR"
                        and not d["published"]
                        and a["patch"] is None
                        and a["probes"] is None
                        and 500 <= e["http_status"] < 600
                        and e["responses"]
                        == a["solver_calls"] + int(bool(a["router_request"]))
                        == a["calls"] - 1
                        and 0 <= a["solver_calls"] <= 15
                        and bool(a["solver_calls"]) == bool(a["first_solver_request"])
                        and e["reserved_nanos"] > 0
                        and e["settled_nanos"] >= 0,
                        "unauthorized missing grade or lost provider error",
                    )
                else:
                    require(
                        type(a["passed"]) is bool
                        and "incident" not in a
                        and a["route"]
                        in ("none", "apply", "skip", "uncertain", "skip_invalid", "skip_memory_budget")
                        and bool(a["selected"]) == (a["route"] == "apply")
                        and (a["route"] == "none") == (not pool)
                        and a["state"] in ("COMPLETE", "FAILED", "EXHAUSTED")
                        and (a["state"] == "COMPLETE") == a["passed"]
                        and (not a["passed"] or a["patch"])
                        and (not a["patch"] or re.fullmatch(r"[0-9a-f]{64}", a["patch"]))
                        and 1 <= a["solver_calls"] <= 15
                        and a["calls"] == a["solver_calls"] + int(bool(pool)),
                        "unbound run, route, budget or grade",
                    )
                probes = a["probes"]
                names = {name[6:-3] for name in task.grader_files if name.startswith("probe_")}
                if names and a["patch"]:
                    require(
                        isinstance(probes, dict)
                        and set(probes) == names | {"public"}
                        and all(type(x) is bool for x in probes.values())
                        and all(probes.values()) == a["passed"],
                        "missing, extra or contradictory repair probe",
                    )
                else:
                    require(probes is None, "unexpected repair probes")
                seen.add(rid)
                ords = details[rid]["ordinals"]
                charge = details[rid]["credit_nanos"]
                require(
                    type(charge) is int
                    and charge >= 0
                    and sorted(ords) == ords
                    and len(ords) == a["calls"]
                    and type(ords[0]) is int
                    and ords[0] > last,
                    "invalid charge or unbalanced arm order",
                )
                reservation = details[rid]["reserved_nanos"]
                pending = details[rid]["open_ordinals"]
                if a["passed"] is None:
                    incident = a["incident"]
                    require(
                        incident["reserved_nanos"] == reservation
                        and incident["settled_nanos"] == charge
                        and pending == [ords[-1]] == [incident["open_ordinal"]],
                        "unbound retained reservation",
                    )
                else:
                    require(reservation == 0 and not pending, "graded arm has an unsettled charge")
                held += reservation
                last = ords[-1]
                subtotal += charge
                positions.extend(ords)
                bucket.extend(ords)
        first = min(min(b) for b in by_batch.values())
        require(first > end, "paid phases overlap")
        # Pilot attempts can overlap across rounds; formal batches are separated before advancing.
        for k, b in by_batch.items():
            if phase != "pilot":
                require(min(b) > end, "later repeat started before previous batch finished")
            end = max(end, max(b))
        costs = d["billing"]["by_phase"][phase]
        require(
            costs["calls"] == sum(len(b) for b in by_batch.values())
            and Decimal(str(costs["reserved_credits"])) * 10**9 == held
            and Decimal(str(costs["cost_credits"])) * 10**9 == subtotal,
            "stage charges differ",
        )
    amount = sum(x["credit_nanos"] for x in details.values())
    total = d["billing"]["total"]
    require(
        set(details) == seen
        and d["billing"]["solves"] == len(seen)
        and sorted(positions) == list(range(1, total["calls"] + 1))
        and Decimal(str(total["reserved_credits"])) * 10**9
        == sum(x["reserved_nanos"] for x in details.values())
        and Decimal(str(total["cost_credits"])) * 10**9 == amount
        and set(d["billing"]["by_phase"]) == {k for k, v in panels(d) if v},
        "incomplete paid ledger",
    )
    return len(seen)


def outcome_summary(d, p):
    rows = d["dev"] + d["support"]
    confirmations = [f["confirmation"]["id"] for f in p["families"].values()]
    objects = p["objects"] + confirmations
    total = {}
    for panel, ids in (("objects", objects), ("configuration", p["config"]), ("support", p["support"])):
        selected = [r for r in rows if r["task"] in ids]
        total[panel] = {arm: sum(r[arm]["passed"] is True for r in selected) for arm in ARMS}
        total[panel]["pairs"] = len(selected)
        total[panel]["graded"] = {a: sum(r[a]["passed"] is not None for r in selected) for a in ARMS}
    evidence = {}
    for tid in objects:
        local = [r for r in rows if r["task"] == tid]
        evidence[tid] = {
            "opportunities": {a: sum(not r[a]["passed"] for r in local) for a in ("parent", "bare")},
            "child_applications": sum(r["portfolio"]["selected"] == CHILD for r in local),
            "attributed": {
                a: d["gate"]["decisions"][a]["tasks"][tid]["attributed"] for a in ("parent", "bare")
            },
        }
    transitions = {}
    for family, stages in p["families"].items():
        trials = {}
        for stage, phase_rows, arms in (
            ("pilot", d["pilot_trials"], ("parent",)),
            ("confirmation", d["dev"], ARMS),
        ):
            tid = stages[stage]["id"]
            local = [r for r in phase_rows if r["task"] == tid]
            observations = {}
            for arm in arms:
                attempts = [r[arm] for r in local]
                submitted = [a["probes"] for a in attempts if a["patch"] and a["probes"]]
                green = [g for g in submitted if g["public"] and g["controls"]]
                targets = sorted(set(stages[stage]["calibration"]["reference"]) - {"public", "controls"})
                observations[arm] = {
                    "passed": sum(a["passed"] for a in attempts),
                    "submitted": len(submitted),
                    "public_and_controls_green": len(green),
                    "still_missed_a_boundary": sum(not all(g.values()) for g in green),
                    "misses_after_green": {k: sum(not g[k] for g in green) for k in targets},
                }
            trials[stage] = {"task": tid, "arms": observations}
        transitions[family] = trials
    return {"panels": total, "objects": evidence, "pilot_to_confirmation": transitions}


def verify(d, p):
    history = check_registration(p)
    require(
        d["completed"] and d["protocol"] == REGISTERED and history["wire_profile"] == d["wire_profile"],
        "different study or GLM settings",
    )
    gate = d["gate"]
    pilot = pilot_summary(p, d["pilot_trials"])
    missing = any(r[a]["passed"] is None for r in d["support"] for a in ARMS)
    if missing:
        body = incomplete_receipt(p, pilot, d["dev"], d["support"])
        passed, library = False, body["library_id"]
        add = read(ADDENDUM)
        require(
            d.get("all_slots_graded") is False
            and add["protocol"] == REGISTERED
            and add["collector"]
            == digest(Path(__file__).with_name("collect_callback_support.py").read_text()),
            "incomplete study was redefined or reported as fully graded",
        )
    else:
        decisions, passed = comparisons(p, pilot, d["dev"] + d["support"])
        library = digest(
            dict(
                schema=2,
                kind="skill_library",
                mode="live",
                members=POOLS["portfolio"],
                route_context="public_shape",
            )
        )
        body = dict(
            schema=1,
            kind="library_evaluation",
            mode="live",
            baseline_library=None,
            baseline_members=[],
            members=POOLS["portfolio"],
            candidate=CHILD,
            library_id=library,
            protocol=d["protocol"],
            pilot=digest(pilot),
            observations=digest(d["dev"] + d["support"]),
            decisions=decisions,
            gate={"policy": "prospective_callback_release_v1", "passed": passed},
        )
    require(
        d["pilot"] == pilot
        and gate == dict(body, receipt=digest(body))
        and passed == d["published"]
        and d["phase"] == ("complete" if passed else "gate_rejected"),
        "different evaluation or publication decision",
    )
    if passed:
        a = d["activation"]
        require(
            a["protocol"] == REGISTERED
            and a["receipt"] == gate["receipt"]
            and a["library"] == library
            and a["transaction"]
            == dict(
                seq=1,
                before_id=None,
                after_id=library,
                evaluation_id=gate["receipt"],
                reason="evaluation promotion",
            )
            and re.fullmatch(r"[0-9a-f]{64}", a["knowledge"]),
            "missing or inconsistent first activation",
        )
    else:
        require(
            d["activation"] is None and not d["holdout_trials"], "release or holdout before a passing gate"
        )
    require(d["summary"] == outcome_summary(d, p), "reported totals differ from paired applications")
    tasks = workspaces(p, passed)
    solves = schedule_and_billing(d, p, history, tasks)
    require(wire_audit(d, tasks) == d["wire_audit"], "invalid request audit")
    pairs, curve = [], []
    if passed:
        for n, family in enumerate(FAMILIES):
            rows = [dict(pair(r, "bare"), family=family) for r in d["holdout_trials"][10 * n : 10 * (n + 1)]]
            pairs.extend(rows)
            curve.append(curve_point(pairs, family, rows))
    require(
        d["holdout"] == pairs
        and d["curve"] == curve
        and d["holdout_exposures"] == len(pairs) == (30 if passed else 0),
        "wrong test membership, denominator or gain",
    )
    replay = patch_inventory(d)
    require(
        d["patch_replay"]
        == {
            "unique_submissions": len(replay),
            "with_named_probes": sum(v[1] is not None for v in replay.values()),
        },
        "different patch replay inventory",
    )
    return dict(
        phase=d["phase"],
        solves=solves,
        receipt=gate["receipt"],
        exposures=len(pairs),
        **d["billing"]["total"],
    )


def patch_inventory(d):
    return observed_patches(
        {"dev": d["pilot_trials"] + d["dev"], "support": d["support"], "holdout_trials": d["holdout_trials"]}
    )


def export(root, ledger, p):
    d = read(root / "result.json")
    require(p == read(root / "protocol.json") and d["completed"], "unfrozen experiment")
    store = Archive(root / "state/evocoder.sqlite3")
    g = d["gate"]
    cfg = Settings(_env_file=None, model=p["model"], image=p["image"])
    rows = [r for _, group in panels(d) for r in group]
    seen = audit_trial_arms(SimpleNamespace(store=store, settings=cfg), scored(rows))
    missed = {r[a]["run"] for r in rows for a in r["order"] if r[a]["passed"] is None}
    require(not seen & missed, "scored run was also marked missing")
    seen |= missed
    options, response_ids = {}, {}
    tasks = workspaces(p, d["published"])
    library = g["library_id"] if d["published"] else None
    require(
        store.active_library() == library
        and store.get_artifact(g["receipt"]) == {k: v for k, v in g.items() if k != "receipt"}
        and set(seen) == {path.stem for path in (root / "slots").glob("*.json")},
        "unreported attempt or invalid deployment",
    )
    with store.connect() as db:
        result = db.execute(
            "SELECT report,passed FROM library_evaluations WHERE id=? AND library_id=? AND baseline_id IS NULL",
            (g["receipt"], g["library_id"]),
        ).fetchone()
        require(
            result
            and json.loads(result[0]) == store.get_artifact(g["receipt"])
            and bool(result[1]) == d["published"],
            "database acceptance differs from evaluation",
        )
        releases = [
            dict(r)
            for r in db.execute("SELECT seq,before_id,after_id,evaluation_id,reason FROM library_releases")
        ]
        require(
            releases == ([d["activation"]["transaction"]] if library else []), "different deployment history"
        )
        require(
            frozen_knowledge(store)
            == (d["activation"]["knowledge"] if library else p["ancestral_knowledge"]),
            "changed skills or releases",
        )
        tested = {x[0] for x in db.execute("SELECT task_id FROM runs WHERE split='test'")} & set(
            p["holdout_hashes"]
        )
    for r in rows:
        for name in r["order"]:
            a = r[name]
            run = store.run(a["run"])
            events = store.events(a["run"])
            task = tasks[r["task"]]
            (start_event,) = [e["payload"] for e in events if e["kind"] == "START"]
            response_ids[a["run"]] = [
                e["payload"]["call_id"] for e in events if e["kind"] == "MODEL_RESPONSE"
            ]
            require(
                start_event["solver_prompt_hash"] == digest(SOLVER_PROMPT)
                and start_event["library_id"] == run["result"]["library_id"]
                and start_event["task"] == task.public(),
                "wrong initial execution binding",
            )
            if a["patch"]:
                (submission,) = [e["payload"] for e in events if e["kind"] == "PATCH"]
                (public,) = [e["payload"]["observation"] for e in events if e["kind"] == "PUBLIC_VERIFY"]
                (grade_event,) = [e["payload"] for e in events if e["kind"] == "GRADE"]
                require(
                    submission["artifact"] == a["patch"]
                    and grade_event["passed"] == a["passed"]
                    and grade_event["grader_hash"]
                    == digest({"files": task.grader_files, "command": task.grader_command})
                    and public["image_id"] == grade_event["image_id"] == p["image"]
                    and not grade_event["timeout"]
                    and not public["timeout"]
                    and (a["probes"] is None or (public["exit_code"] == 0) == a["probes"]["public"]),
                    "submitted patch or original public/hidden grade receipt differs",
                )
            intent = read(root / "slots" / (a["run"] + ".json"))
            require(
                digest(intent)[:32] == a["run"]
                and digest(run["config"]) == intent["options"]
                and run["config"]["max_steps"] == (16 if a["pool"] else 15)
                and run["result"]["library_id"]
                == (library if name == "portfolio" and r["split"] == "test" else None),
                "mismatched archived options or published treatment",
            )
            require(
                options.setdefault(name, intent["options"]) == intent["options"],
                "changed treatment parameters",
            )
            if a["passed"] is None:
                scoped = SimpleNamespace(
                    settings=Settings(_env_file=None, **run["config"]),
                    protocol=d["protocol"],
                    root=root,
                    store=store,
                    ledger=Archive(ledger),
                )
                require(
                    ungraded(scoped, task, r["repeat"], name) == a, "reported missing slot differs from SQL"
                )
            if a["pool"] and a["route"] is not None:
                audit_selection(store, events, a, a["route"])
    history = read(PREVIOUS_RESULT)
    require(options == {a: history["slot_options"][a] for a in ARMS}, "different options from calibration")
    first = store.run(rows[0]["parent"]["run"])["config"]
    d["wire_profile"] = {k: first[k] for k in WIRE_FIELDS}
    start = read(root / "start.json")["cursor"]
    with Archive(ledger).connect() as db:
        charges = {}
        for row in db.execute(
            "SELECT rowid,id,run_id,actual,state,reserved FROM calls WHERE rowid>? ORDER BY rowid", (start,)
        ):
            if row["run_id"] not in seen:
                continue
            rid = row["run_id"]
            ordinal = row["rowid"] - start
            bill = charges.setdefault(
                rid, dict(ordinals=[], credit_nanos=0, reserved_nanos=0, open_ordinals=[])
            )
            if row["state"] == "reserved":
                require(
                    rid in missed
                    and len(bill["ordinals"]) == len(response_ids[rid])
                    and row["actual"] is None,
                    "unrecognized unsettled call",
                )
                bill["reserved_nanos"] += row["reserved"]
                bill["open_ordinals"].append(ordinal)
            else:
                require(
                    row["state"] == "settled"
                    and row["actual"] is not None
                    and response_ids[rid][len(bill["ordinals"])] == row["id"],
                    "model reply does not own its settled ledger call",
                )
                bill["credit_nanos"] += row["actual"]
            bill["ordinals"].append(ordinal)
        exposed = {
            x[0]: tuple(x[1:])
            for x in db.execute("SELECT task_id,task_hash,experiment FROM test_exposures")
            if x[0] in p["holdout_hashes"]
        }
    require(
        tested == {r["task"] for r in d["holdout_trials"]}
        and exposed == {t: (p["holdout_hashes"][t], REGISTERED) for t in tested},
        "unreported held-out execution",
    )
    d["charges"] = charges
    patches = patch_inventory(d)

    def replay(item):
        (tid, key), (passed, probes) = item
        t, patch = tasks[tid], store.get_artifact(key)
        require(digest(patch) == key and set(patch) == set(t.editable), "wrong submitted files")
        if probes:
            require(probe_patch(t, patch, cfg) == probes, "named grade failed independent replay")
        else:
            pub = verify_public(t, patch, cfg, "callback-audit")
            hidden = grade(t, patch, cfg, "callback-audit")
            require(
                pub["image_id"] == hidden["image_id"] == cfg.image
                and not pub["timeout"]
                and not hidden["timeout"]
                and ((pub["exit_code"] == 0 and hidden["passed"]) == passed),
                "grade failed independent replay",
            )

    bounded_map(replay, list(patches.items()), 4)
    d["patch_replay"] = {
        "unique_submissions": len(patches),
        "with_named_probes": sum(v[1] is not None for v in patches.values()),
    }
    d["wire_audit"] = wire_audit(d, tasks)
    d["summary"] = outcome_summary(d, p)
    verify(d, p)
    return d


def calibrate(p):
    cfg = Settings(_env_file=None, image=p["image"])
    for family, stages in sources().items():
        for stage, (t, fix) in stages.items():
            first = t.editable[0]
            direct = {path: fix[path] if path == first else t.files[path] for path in t.editable}
            actual = {
                name: probe_patch(t, patch, cfg)
                for name, patch in (("broken", {}), ("first_file_only", direct), ("reference", fix))
            }
            require(actual == p["families"][family][stage]["calibration"], "changed Docker calibration")
    return "Replayed four workspaces, three patches each."


if __name__ == "__main__":
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("command", choices=["verify", "export", "calibrate"])
    cli.add_argument("--root", type=Path, default=ROOT)
    cli.add_argument("--ledger", type=Path)
    cli.add_argument("--file", type=Path, default=RESULT)
    args = cli.parse_args()
    p = read(PUBLIC)
    if args.command == "export":
        if args.ledger is None:
            cli.error("export requires the original --ledger SQLite path")
        data = export(args.root, args.ledger, p)
        dump(args.file, data)
        print(verify(data, p))
    elif args.command == "calibrate":
        check_registration(p)
        print(calibrate(p))
    else:
        print(verify(read(args.file), p))
