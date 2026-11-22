"""Complete only still-unattempted, originally assigned support slots after a documented provider error."""

import argparse
import re
from pathlib import Path

from callback_protocol import ARMS, PUBLIC, arm_order, cell, session
from evocoder.contracts import digest
from evocoder.jobs import bounded_map
from portfolio_routing import PARENT, POOLS, config
from report_click_procedure import read
from run_callback_release import checked_pilot, design, read_batch
from run_falsy_transfer import atomic_trial, require
from run_parallel_routing import read_arm
from run_shape_routing import audit_trial_arms

ADDENDUM = Path("benchmarks/falsy_transfer/callback-collection.json")
ORIGINAL = "1ec246da6a813d0b1386f966d86d7e4bf96f43db9359e6231151da0894925c28"
INCIDENT = "e011f7b165adb49b2fb054b9996f90a9"


def intent(s, t, n, arm):
    pool = {"parent": [PARENT], "portfolio": POOLS["portfolio"], "bare": []}[arm]
    view = "public_shape" if arm == "portfolio" else "issue"
    opts = s.settings.model_copy(update={"library_route_context": view, "max_steps": 16 if pool else 15})
    return dict(
        protocol=s.protocol,
        phase="gate",
        task=t.id,
        task_hash=digest(t.model_dump()),
        repeat=n,
        pool=pool,
        oracle=False,
        options=digest(opts.public()),
    ), view


def ungraded(s, t, n, arm):
    slot, view = intent(s, t, n, arm)
    rid = digest(slot)[:32]
    require(slot == read(s.root / "slots" / (rid + ".json")), "missing original intent")
    run = s.store.run(rid)
    ev = s.store.events(rid)
    cost = run["result"]["cost"]
    err = run["result"].get("error", "")
    status = re.fullmatch(r"RuntimeError: model_http_(5[0-9][0-9]): reservation retained", err)
    with s.ledger.connect() as db:
        charges = list(
            db.execute(
                "SELECT rowid,id,state,reserved,actual FROM calls WHERE run_id=? ORDER BY rowid", (rid,)
            )
        )
    replies = [e["payload"] for e in ev if e["kind"] == "MODEL_RESPONSE"]
    routers = [r for r in replies if r["expected_function"] == "select_library_skill"]
    solvers = [r for r in replies if r["expected_function"] == "submit_action"]
    route = [e["payload"] for e in ev if e["kind"] == "SKILL_ROUTE"]
    require(
        status
        and run["state"] == "ERROR"
        and run["mode"] == "live"
        and run["purpose"] == "evaluate"
        and run["split"] == t.split == "learn"
        and run["task_id"] == t.id
        and run["task_hash"] == slot["task_hash"]
        and run["result"]["patch"] is None
        and not any(e["kind"] in ("GRADE", "PATCH") for e in ev)
        and len(charges) == len(replies) + 1 == cost["calls"]
        and charges[-1]["state"] == "reserved"
        and all(
            c["state"] == "settled" and c["id"] == r["call_id"]
            for c, r in zip(charges[:-1], replies, strict=True)
        )
        and all(r["provider_model"].lower() == s.settings.model.lower() for r in replies)
        and len(route) <= 1
        and len(routers) <= int(bool(slot["pool"])),
        "unrecognized failure or scored repair",
    )
    route = route[0] if route else {}
    cursor = read(s.root / "start.json")["cursor"]
    return dict(
        run=rid,
        passed=None,
        state="ERROR",
        patch=None,
        probes=None,
        route=route.get("decision"),
        selected=route.get("applied_skill_id"),
        pool=slot["pool"],
        view=view,
        reason=route.get("reason", ""),
        quotes=route.get("task_quotes", []),
        conditions=route.get("condition_indices", []),
        first_solver_request=solvers[0]["request_body_hash"] if solvers else None,
        router_request=routers[0]["request_body_hash"] if routers else None,
        solver_calls=len(solvers),
        calls=cost["calls"],
        image=run["result"]["image_id"],
        incident={
            "http_status": int(status[1]),
            "responses": len(replies),
            "open_ordinal": charges[-1]["rowid"] - cursor,
            "reserved_nanos": charges[-1]["reserved"],
            "settled_nanos": sum(c["actual"] for c in charges[:-1]),
        },
    )


def scored(rows):
    return [dict(r, order=[a for a in r["order"] if r[a]["passed"] is not None]) for r in rows]


def collect(s, tid, n):
    t = s.task(tid)
    file = s.root / "trials" / f"support-{tid}-{n}.json"
    seq = arm_order(tid, n, ARMS)
    if file.exists():
        return cell(s, "support", tid, n, ARMS)
    r = dict(
        task=tid, snapshot=digest(t.model_dump()), group=t.source_group, split=t.split, repeat=n, order=seq
    )
    for arm in seq:
        slot, _ = intent(s, t, n, arm)
        rid = digest(slot)[:32]
        with s.store.connect() as db:
            old = db.execute("SELECT state FROM runs WHERE id=?", (rid,)).fetchone()
        if old and old[0] == "ERROR":
            r[arm] = ungraded(s, t, n, arm)
        else:
            try:
                r[arm] = read_arm(s, t, n, arm)
            except RuntimeError:
                r[arm] = ungraded(s, t, n, arm)
    atomic_trial(file, r)
    print("support", n + 1, tid, [(a, r[a]["passed"]) for a in seq], flush=True)
    return r


def recover(s, n):
    checked_pilot(s)
    read_batch(s, "gate", 6)
    if n:
        read_batch(s, "support", n - 1)
    extra = read(ADDENDUM)
    initial = ungraded(s, s.task("export-04"), 0, "parent")
    require(
        s.protocol == ORIGINAL == extra["protocol"]
        and initial["run"] == INCIDENT == extra["first_missing"]
        and extra["collector"] == digest(Path(__file__).read_text()),
        "different recovery boundary",
    )
    ids, _ = design(s.p, "support", n)
    rows = bounded_map(lambda tid: collect(s, tid, n), ids, s.p["workers"])
    audit_trial_arms(s, scored(rows))
    for r in rows:
        for a in r["order"]:
            if r[a]["passed"] is None:
                require(r[a] == ungraded(s, s.task(r["task"]), n, a), "ungraded evidence changed")
    print(
        "recorded",
        n + 1,
        "graded",
        sum(r[a]["passed"] is not None for r in rows for a in ARMS),
        "/",
        len(rows) * 3,
        flush=True,
    )


if __name__ == "__main__":
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--round", type=int, choices=[1, 2, 3], required=True)
    args = cli.parse_args()
    with session(read(PUBLIC), config(Path(".env.glm"))) as study:
        recover(study, args.round - 1)
