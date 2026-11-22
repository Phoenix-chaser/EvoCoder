"""Issue a rejecting receipt with missing support grades kept explicitly unknown."""

import json
from pathlib import Path

from callback_policy import comparisons
from callback_protocol import ARMS, PUBLIC, session
from collect_callback_support import ADDENDUM, INCIDENT, scored, ungraded
from evocoder.comparison import frozen_knowledge
from evocoder.contracts import canonical, digest
from evocoder.library import SkillLibrary
from portfolio_routing import CHILD, POOLS, config
from report_click_procedure import read
from run_callback_release import checked_pilot, formal
from run_falsy_transfer import atomic_trial, require
from run_shape_routing import audit_trial_arms, pair


def incomplete_receipt(p, pilot, dev, support):
    missing = [
        dict(task=r["task"], repeat=r["repeat"], arm=a, run=r[a]["run"], **r[a]["incident"])
        for r in support
        for a in ARMS
        if r[a]["passed"] is None
    ]
    require(
        missing
        and INCIDENT in {x["run"] for x in missing}
        and all(x["task"] in p["support"] for x in missing)
        and all(r[a]["passed"] is not None for r in dev for a in ARMS),
        "only incomplete original support can close here",
    )
    affected = {x["task"] for x in missing}
    partial = [r for r in support if r["task"] in affected]
    subset = dict(
        p,
        protected={k: v for k, v in p["protected"].items() if k not in affected},
        support=[k for k in p["support"] if k not in affected],
    )
    decisions, _ = comparisons(subset, pilot, dev + [r for r in support if r["task"] not in affected])
    coverage = {}
    for t in sorted(affected):
        coverage[t] = {}
        for base in ("parent", "bare"):
            available = [
                r
                for r in partial
                if r["task"] == t and r[base]["passed"] is not None and r["portfolio"]["passed"] is not None
            ]
            pairs = [pair(r, base) for r in available]
            coverage[t][base] = dict(
                planned=3,
                graded_rounds=[r["repeat"] + 1 for r in available],
                baseline_passed=sum(r["baseline_pass"] for r in pairs),
                candidate_passed=sum(r["candidate_pass"] for r in pairs),
                changed_loss=sum(
                    r["baseline_request"] != r["candidate_request"]
                    and r["baseline_pass"]
                    and not r["candidate_pass"]
                    for r in pairs
                ),
                changed_win=sum(
                    r["baseline_request"] != r["candidate_request"]
                    and not r["baseline_pass"]
                    and r["candidate_pass"]
                    for r in pairs
                ),
            )
    target = digest(
        dict(
            schema=2,
            kind="skill_library",
            mode="live",
            members=POOLS["portfolio"],
            route_context="public_shape",
        )
    )
    return dict(
        schema=2,
        kind="library_evaluation",
        mode="live",
        baseline_library=None,
        baseline_members=[],
        members=POOLS["portfolio"],
        candidate=CHILD,
        library_id=target,
        protocol=digest(p),
        pilot=digest(pilot),
        observations=digest(dev + support),
        decisions=decisions,
        ungraded_support=missing,
        affected_support_pairs=coverage,
        collection=digest(read(ADDENDUM)),
        gate=dict(policy="prospective_callback_release_v1", passed=False, complete_evidence=False),
    )


def billing(s, panels):
    ids = set()
    stages = {}
    for phase, rows in panels:
        keys = []
        for r in rows:
            for a in r["order"]:
                x = r[a]
                rid = x["run"]
                intent = read(s.root / "slots" / (rid + ".json"))
                require(
                    rid == digest(intent)[:32]
                    and intent["protocol"] == s.protocol
                    and rid not in ids
                    and intent["task"] == r["task"]
                    and intent["repeat"] == r["repeat"]
                    and intent["task_hash"] == r["snapshot"]
                    and intent["pool"] == x["pool"],
                    "substituted or extra attempt",
                )
                keys.append(rid)
                ids.add(rid)
        stages[phase] = s.ledger.costs(keys)
    start = read(s.root / "start.json")
    with s.ledger.connect() as db:
        current = {
            r[0] for r in db.execute("SELECT DISTINCT run_id FROM calls WHERE rowid>?", (start["cursor"],))
        }
        open_ids = {
            r[0]
            for r in db.execute(
                "SELECT run_id FROM calls WHERE rowid>? AND state='reserved'", (start["cursor"],)
            )
        }
        old = [
            tuple(r)
            for r in db.execute(
                "SELECT id,reserved FROM calls WHERE rowid<=? AND state='reserved' ORDER BY rowid",
                (start["cursor"],),
            )
        ]
    missing = {r[a]["run"] for _, rows in panels for r in rows for a in r["order"] if r[a]["passed"] is None}
    require(
        ids == current == {x.stem for x in (s.root / "slots").glob("*.json")}
        and open_ids == missing
        and len(old) == start["old_reservations"]
        and digest(old) == start["old_reservations_hash"],
        "foreign charge or changed reservation",
    )
    return dict(by_phase=stages, total=s.ledger.costs(sorted(ids)), solves=len(ids))


def close(s):
    pilot, p = checked_pilot(s)
    dev, support = formal(s)
    audit_trial_arms(s, scored(pilot + dev + support))
    for r in support:
        for a in r["order"]:
            if r[a]["passed"] is None:
                require(ungraded(s, s.task(r["task"]), r["repeat"], a) == r[a], "unbound interruption")
    library = SkillLibrary(s.settings, s.store, runner=object())
    require(
        s.store.active_library() is None
        and frozen_knowledge(s.store) == s.p["ancestral_knowledge"]
        and library.retain_parent([], CHILD) == POOLS["portfolio"]
        and library.provenance(CHILD)["support"]
        == {tid: s.p["protected"][tid]["snapshot"] for tid in s.p["support"]},
        "changed deployment or ancestry",
    )
    bill = billing(s, [("pilot", pilot), ("gate", dev), ("support", support)])
    body = incomplete_receipt(s.p, p, dev, support)
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
    require(target == body["library_id"], "different intended manifest")
    key = s.store.artifact("library_evaluation", body)
    with s.store.connect(write=True) as db:
        db.execute(
            "INSERT OR IGNORE INTO library_evaluations VALUES(?,?,?,?,0)",
            (key, target, None, canonical(body)),
        )
        rec = db.execute("SELECT report,passed FROM library_evaluations WHERE id=?", (key,)).fetchone()
        require(
            rec
            and json.loads(rec[0]) == body
            and not rec[1]
            and db.execute("SELECT COUNT(*) FROM library_releases").fetchone()[0] == 0,
            "non-rejecting or inconsistent deployment",
        )
        tested = {x[0] for x in db.execute("SELECT task_id FROM runs WHERE split='test'")}
    with s.ledger.connect() as db:
        exposed = {x[0] for x in db.execute("SELECT task_id FROM test_exposures")}
    require(
        not (exposed | tested) & set(s.p["holdout_hashes"]) and not (s.root / "activation.json").exists(),
        "opened sealed test",
    )
    gate = dict(body, receipt=key)
    atomic_trial(s.root / "gate.json", gate)
    d = dict(
        protocol=s.protocol,
        completed=True,
        all_slots_graded=False,
        phase="gate_rejected",
        pilot_trials=pilot,
        pilot=p,
        dev=dev,
        support=support,
        gate=gate,
        published=False,
        activation=None,
        holdout_trials=[],
        holdout=[],
        curve=[],
        holdout_exposures=0,
        billing=bill,
    )
    atomic_trial(s.root / "result.json", d)
    print(
        d["phase"],
        "missing",
        len(body["ungraded_support"]),
        "receipt",
        key,
        "solves",
        bill["solves"],
        bill["total"],
        flush=True,
    )


if __name__ == "__main__":
    with session(read(PUBLIC), config(Path(".env.glm"))) as study:
        close(study)
