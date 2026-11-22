"""Run one bounded, resumable batch of the prospectively registered live release study."""

import argparse
from pathlib import Path

from callback_policy import comparisons, pilot_summary
from callback_protocol import ARMS, PUBLIC, arm_order, cell, pilot_rows, session, validate_activation
from evocoder.comparison import frozen_knowledge
from evocoder.contracts import canonical, digest
from evocoder.jobs import bounded_map
from evocoder.library import SkillLibrary
from falsy_transfer_protocol import FAMILIES
from portfolio_routing import CHILD, POOLS, config
from report_click_procedure import read
from run_falsy_transfer import atomic_trial, require
from run_optional_control import curve_point
from run_shape_routing import audit_trial_arms, pair


def design(p, phase, n):
    if phase == "gate" and 0 <= n < 7:
        objects = p["objects"] + [f["confirmation"]["id"] for f in p["families"].values()]
        return (objects if n < 5 else []) + p["config"], ARMS
    if phase == "support" and 0 <= n < 3:
        return p["support"], ARMS
    if phase == "holdout" and 0 <= n < len(FAMILIES):
        return p["holdout"][FAMILIES[n]], ("bare", "portfolio")
    raise ValueError("batch outside preregistration")


def read_batch(s, phase, n):
    ids, arms = design(s.p, phase, n)
    repeat = 0 if phase == "holdout" else n
    rows = []
    for tid in ids:
        row = read(s.root / "trials" / f"{phase}-{tid}-{repeat}.json")
        t = s.task(tid)
        require(
            (row["task"], row["group"], row["split"], row["snapshot"], row["repeat"], row["order"])
            == (tid, t.source_group, t.split, digest(t.model_dump()), repeat, arm_order(tid, repeat, arms)),
            "changed batch identity, assignment or source",
        )
        rows.append(row)
    return rows


def checked_pilot(s):
    rows = pilot_rows(s)
    result = pilot_summary(s.p, rows)
    require(result == read(s.root / "pilot.json"), "pilot eligibility was not frozen before confirmation")
    return rows, result


def batch(s, phase, n):
    checked_pilot(s)
    ids, arms = design(s.p, phase, n)
    if phase == "holdout":
        validate_activation(s)
    if phase == "support":
        read_batch(s, "gate", 6)
    if n:
        read_batch(s, phase, n - 1)
    repeat = 0 if phase == "holdout" else n
    rows = bounded_map(lambda tid: cell(s, phase, tid, repeat, arms), ids, s.p["workers"])
    audit_trial_arms(s, rows)
    print(phase, n + 1, {arm: sum(row[arm]["passed"] for row in rows) for arm in arms}, flush=True)


def audit_billing(s, panels):
    start = read(s.root / "start.json")
    ids = set()
    stages = {}
    cursor = start["cursor"]
    for phase, rows in panels:
        entries = []
        for row in rows:
            for name in row["order"]:
                arm = row[name]
                rid = arm["run"]
                intent = read(s.root / "slots" / (rid + ".json"))
                require(
                    rid == digest(intent)[:32]
                    and rid not in ids
                    and intent["protocol"] == s.protocol
                    and intent["task"] == row["task"]
                    and intent["repeat"] == row["repeat"]
                    and intent["task_hash"] == row["snapshot"]
                    and intent["pool"] == arm["pool"]
                    and intent["phase"]
                    == (
                        "published-holdout"
                        if phase == "holdout" and name == "portfolio"
                        else ("gate" if phase == "support" else phase)
                    ),
                    "reused or unbound paid execution",
                )
                ids.add(rid)
                entries.append((rid, arm["calls"]))
        if not entries:
            continue
        claimed = {rid for rid, _ in entries}
        with s.ledger.connect() as db:
            calls = list(
                db.execute(
                    f"SELECT rowid,run_id,state FROM calls WHERE run_id IN ({','.join('?' for _ in claimed)}) ORDER BY rowid",
                    sorted(claimed),
                )
            )
        require(
            calls
            and all(r["state"] == "settled" for r in calls)
            and calls[0]["rowid"] > cursor
            and len(calls) == sum(cnt for _, cnt in entries),
            "unsettled calls, mixed phases or unaccounted attempts",
        )
        cursor = calls[-1]["rowid"]
        stages[phase] = s.ledger.costs(sorted(claimed))
    with s.ledger.connect() as db:
        current = {
            r[0] for r in db.execute("SELECT DISTINCT run_id FROM calls WHERE rowid>?", (start["cursor"],))
        }
        original = [
            tuple(r)
            for r in db.execute(
                "SELECT id,reserved FROM calls WHERE state='reserved' AND rowid<=? ORDER BY rowid",
                (start["cursor"],),
            )
        ]
    require(
        current == ids == {f.stem for f in (s.root / "slots").glob("*.json")}
        and len(original) == start["old_reservations"]
        and digest(original) == start["old_reservations_hash"],
        "missing or foreign sessions, or changed historical liability",
    )
    return {"by_phase": stages, "total": s.ledger.costs(sorted(ids)), "solves": len(ids)}


def formal(s):
    dev = [r for n in range(7) for r in read_batch(s, "gate", n)]
    support = [r for n in range(3) for r in read_batch(s, "support", n)]
    return dev, support


def issue(s):
    runs, pilot = checked_pilot(s)
    dev, support = formal(s)
    audit_trial_arms(s, runs + dev + support)
    audit_billing(s, [("pilot", runs), ("gate", dev), ("support", support)])
    lib = SkillLibrary(s.settings, s.store, runner=object())
    require(
        lib.retain_parent([], CHILD) == POOLS["portfolio"]
        and lib.provenance(CHILD)["support"]
        == {tid: s.p["protected"][tid]["snapshot"] for tid in s.p["support"]}
        and not {r["group"] for r in dev} & {r["group"] for r in support + runs},
        "different ancestry or overlapping development and learning sources",
    )
    decisions, passed = comparisons(s.p, pilot, dev + support)
    target = s.store.artifact(
        "skill_library",
        dict(
            schema=2,
            kind="skill_library",
            mode="live",
            members=POOLS["portfolio"],
            route_context="public_shape",
        ),
    )
    receipt = dict(
        schema=1,
        kind="library_evaluation",
        mode="live",
        baseline_library=None,
        baseline_members=[],
        members=POOLS["portfolio"],
        candidate=CHILD,
        library_id=target,
        protocol=s.protocol,
        pilot=digest(pilot),
        observations=digest(dev + support),
        decisions=decisions,
        gate=dict(policy="prospective_callback_release_v1", passed=passed),
    )
    key = s.store.artifact("library_evaluation", receipt)
    with s.store.connect(write=True) as db:
        db.execute(
            "INSERT OR IGNORE INTO library_evaluations VALUES(?,?,?,?,?)",
            (key, target, None, canonical(receipt), int(passed)),
        )
        stored = db.execute("SELECT report,passed FROM library_evaluations WHERE id=?", (key,)).fetchone()
        require(tuple(stored) == (canonical(receipt), int(passed)), "different database evaluation")
    gate = dict(receipt, receipt=key)
    atomic_trial(s.root / "gate.json", gate)
    if passed:
        if s.store.active_library() is None:
            require(frozen_knowledge(s.store) == s.p["ancestral_knowledge"], "deployment changed")
            s.store.activate_library(target, key, expected=None, mode="live")
        with s.store.connect() as db:
            (release,) = [
                dict(r)
                for r in db.execute(
                    "SELECT seq,before_id,after_id,evaluation_id,reason FROM library_releases"
                )
            ]
        require(
            s.store.active_library() == target
            and release["before_id"] is None
            and (release["after_id"], release["evaluation_id"]) == (target, key),
            "incorrect activation transition",
        )
        atomic_trial(
            s.root / "activation.json",
            dict(
                protocol=s.protocol,
                library=target,
                receipt=key,
                transaction=release,
                knowledge=frozen_knowledge(s.store),
            ),
        )
        validate_activation(s)
    else:
        require(
            s.store.active_library() is None and frozen_knowledge(s.store) == s.p["ancestral_knowledge"],
            "deployment changed after rejection",
        )
    print(
        "gate",
        passed,
        key,
        {
            b: (v["confirmed_object_sources"], v["regressions"], v["raw_config_panel"]["veto"])
            for b, v in decisions.items()
        },
        flush=True,
    )
    return gate


def finalize(s):
    runs, pilot = checked_pilot(s)
    dev, support = formal(s)
    gate = read(s.root / "gate.json")
    require(
        s.store.get_artifact(gate["receipt"]) == {k: v for k, v in gate.items() if k != "receipt"}
        and (gate["decisions"], gate["gate"]["passed"]) == comparisons(s.p, pilot, dev + support)
        and gate["observations"] == digest(dev + support)
        and gate["pilot"] == digest(pilot)
        and gate["protocol"] == s.protocol,
        "inconsistent terminal evaluation",
    )
    passed = gate["gate"]["passed"]
    activation = None
    trials, holdout, curve = [], [], []
    if passed:
        active = validate_activation(s)
        activation = read(s.root / "activation.json")
        for n, family in enumerate(FAMILIES):
            group = read_batch(s, "holdout", n)
            for r in group:
                require(
                    s.store.run(r["portfolio"]["run"])["result"]["library_id"] == active
                    and s.store.run(r["bare"]["run"])["result"]["library_id"] is None,
                    "unpublished or substituted test treatment",
                )
            trials.extend(group)
            pairs = [dict(pair(r, "bare"), family=family) for r in group]
            holdout.extend(pairs)
            curve.append(curve_point(holdout, family, pairs))
    else:
        require(
            s.store.active_library() is None and not (s.root / "activation.json").exists(),
            "release without approval",
        )
    audit_trial_arms(s, runs + dev + support + trials)
    billing = audit_billing(s, [("pilot", runs), ("gate", dev), ("support", support), ("holdout", trials)])
    all_test = set(s.p["holdout_hashes"])
    expected = {r["task"] for r in trials}
    with s.ledger.connect() as db:
        exposed = {
            r[0]: tuple(r[1:])
            for r in db.execute("SELECT task_id,task_hash,experiment FROM test_exposures")
            if r[0] in all_test
        }
    with s.store.connect() as db:
        tested = {r[0] for r in db.execute("SELECT task_id FROM runs WHERE split='test'")} & all_test
    require(
        tested == expected
        and exposed == {t: (s.p["holdout_hashes"][t], s.protocol) for t in expected}
        and len(expected) == (30 if passed else 0),
        "unreported test exposures",
    )
    result = dict(
        protocol=s.protocol,
        completed=True,
        phase="complete" if passed else "gate_rejected",
        pilot_trials=runs,
        pilot=pilot,
        dev=dev,
        support=support,
        gate=gate,
        published=passed,
        activation=activation,
        holdout_trials=trials,
        holdout=holdout,
        curve=curve,
        holdout_exposures=len(exposed),
        billing=billing,
    )
    atomic_trial(s.root / "result.json", result)
    print(result["phase"], "tested", len(exposed), billing, flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=["gate", "support", "decide", "holdout", "finalize"])
    parser.add_argument("--round", type=int, help="One-based round or holdout stratum (scalar/layers/batch)")
    args = parser.parse_args()
    with session(read(PUBLIC), config(Path(".env.glm"))) as study:
        if args.phase in ("decide", "finalize"):
            require(args.round is None, "decision takes no round")
            (issue if args.phase == "decide" else finalize)(study)
        else:
            require(args.round is not None and args.round >= 1, "positive --round required")
            batch(study, args.phase, args.round - 1)
