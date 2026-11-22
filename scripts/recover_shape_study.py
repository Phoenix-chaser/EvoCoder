"""Finish unstarted support arms after a host interruption, preserving missing originals and open charges."""

import argparse
from pathlib import Path

from evocoder.comparison import dump
from evocoder.contracts import canonical, digest
from evocoder.runtime import grade, verify_public
from portfolio_routing import CHILD, POOLS, config
from report_click_procedure import read
from run_falsy_transfer import atomic_trial, require
from run_parallel_routing import PortfolioStudy
from run_shape_routing import audit_trial_arms, collect, decisions
from shape_study import ROOT, SEED, route_receipts, session

INCIDENT = Path("benchmarks/falsy_transfer/shape-routing-interruption.json")
REPLAY_PHASE = "gate-host-interruption-independent-replay"


def record(root, p, st, ledger):
    with st.connect() as db:
        running = sorted(
            r[0]
            for r in db.execute("SELECT id FROM runs WHERE state='RUNNING'")
            if (root / "slots" / (r[0] + ".json")).is_file()
        )
    start = read(root / "start.json")["cursor"]
    with ledger.connect() as db:
        last = db.execute("SELECT max(rowid) FROM calls").fetchone()[0] - start
    affected = []
    for rid in running:
        run, events = st.run(rid), st.events(rid)
        intent = read(root / "slots" / (rid + ".json"))
        require(
            run["split"] == "learn" and intent["repeat"] == 2 and intent["task"] in p["support"],
            "interruption outside support",
        )
        cost = ledger.costs([rid])
        with ledger.connect() as db:
            calls = db.execute(
                "SELECT rowid,state FROM calls WHERE run_id=? ORDER BY rowid", (rid,)
            ).fetchall()
        patch = next((e["payload"]["artifact"] for e in events if e["kind"] == "PATCH"), None)
        actions = [e["payload"]["tool"] for e in events if e["kind"] == "ACTION"]
        open_calls = [r["rowid"] - start for r in calls if r["state"] == "reserved"]
        if patch:
            require(
                actions[-1] == "finish" and events[-1]["kind"] == "PUBLIC_VERIFY" and not open_calls,
                "ambiguous submitted patch",
            )
        else:
            require(
                len(open_calls) == 1 and events[-1]["kind"] == "OBSERVATION",
                "unexpected missing-grade evidence",
            )
        affected.append(
            dict(
                run=rid,
                intent=intent,
                events_hash=digest(events),
                event_count=len(events),
                last_event=events[-1]["kind"],
                patch=patch,
                original_grade_available=bool(patch),
                cost=cost,
                open_call_ordinals=open_calls,
            )
        )
    trials = [f for f in (root / "trials").glob("*.json")]
    require(
        len(affected) == 4
        and sum(bool(x["patch"]) for x in affected) == 1
        and len([f for f in trials if f.name.startswith("dev-")]) == 35
        and len([f for f in trials if f.name.startswith("support-")]) == 16,
        "unexpected incident population",
    )
    return dict(
        protocol=digest(p),
        event="host_process_terminated_during_third_support_round",
        last_call_ordinal=last,
        existing_slots=len(list((root / "slots").glob("*.json"))),
        affected=affected,
        treatment="Complete the archived finish without a model call. Keep the three missing original grades and outstanding "
        "charges explicit. For descriptive support results only, collect one separately identified fresh solve for each "
        "unreturned arm, with the same task, model, routing, solver allowance and order. Reuse all completed original "
        "slots and run all unstarted original slots. No supplemental outcome authorizes a release. The original gate "
        "is blocked on infrastructure and accounting as well as independent complete dev evidence. Never open the holdout.",
    )


class RecoveryStudy(PortfolioStudy):
    def solve(self, task, phase, repeat=0, *, pool=None, view=None):
        for x in self.incident["affected"]:
            slot = x["intent"]
            if not x["patch"] and (task.id, repeat, pool or []) == (
                slot["task"],
                slot["repeat"],
                slot["pool"],
            ):
                require(
                    phase == "gate" and task.split == "learn",
                    "replay may only fill this descriptive support cell",
                )
                phase = REPLAY_PHASE
        return super().solve(task, phase, repeat, pool=pool, view=view)


def finalize_originals(study, incident):
    st = study.store
    for x in incident["affected"]:
        rid = x["run"]
        events = st.events(rid)
        require(digest(events[: x["event_count"]]) == x["events_hash"], "changed original trace")
        if st.run(rid)["state"] != "RUNNING":
            continue
        if not x["patch"]:
            st.finish(
                rid,
                "INTERRUPTED",
                {
                    "repair_grade": None,
                    "error": "host_terminated_before_response",
                    "unsettled_calls": x["open_call_ordinals"],
                },
            )
            continue
        t = study.tasks[x["intent"]["task"]]
        patch = st.get_artifact(x["patch"])
        cfg = study.settings
        (public,) = [e["payload"]["observation"] for e in events if e["kind"] == "PUBLIC_VERIFY"]
        check = verify_public(t, patch, cfg, rid)
        hidden = grade(t, patch, cfg, rid)
        require(
            check["exit_code"] == public["exit_code"]
            and not check["timeout"]
            and not hidden["timeout"]
            and public["image_id"] == check["image_id"] == hidden["image_id"] == cfg.image,
            "recovered patch environment differs",
        )
        final = bool(hidden["passed"] and check["exit_code"] == 0)
        if not any(e["kind"] == "GRADE" for e in events):
            st.event(rid, "GRADE", {**hidden, "passed": final})
        observations = [e["payload"] for e in events if e["kind"] == "OBSERVATION"]
        (route,) = [e["payload"] for e in events if e["kind"] == "SKILL_ROUTE"]
        st.finish(
            rid,
            "COMPLETE" if final else "FAILED",
            dict(
                passed=final,
                patch=x["patch"],
                image_id=cfg.image,
                requested_skill_ids=route["requested_skill_ids"],
                applied_skill_id=route["applied_skill_id"],
                routing=route,
                steps=len(observations),
                model_steps=sum(e["kind"] == "MODEL_RESPONSE" for e in events),
                rejected_actions=sum(e["kind"] == "ACTION_REJECTED" for e in events),
                public_failed=any(
                    o["action"]["tool"] == "test" and o["observation"].get("exit_code") != 0
                    for o in observations
                ),
                cost=st.costs([rid]),
                seconds=None,
                error=None,
                exhaustion_reason=None,
                library_id=None,
                experience_pool_id=None,
                requested_skill_id=None,
                grade_recovered_from_submitted_patch=True,
            ),
        )


def recover(study, p, incident):
    root = study.root
    if (root / "result.json").exists():
        return read(root / "result.json")
    require(
        incident == read(root / "interruption.json") and incident["protocol"] == study.protocol,
        "different incident",
    )
    study.incident, study.selected = incident, "public_shape"
    finalize_originals(study, incident)
    dev, support = collect(study, p, "dev"), collect(study, p, "support")
    rows = dev + support
    audit_trial_arms(study, rows)
    expected = {r["task"]: (r["split"], r["group"], r["snapshot"]) for r in rows}
    comps, would_pass = decisions(p, rows, expected)
    require(
        not comps["objects_vs_parent"]["gate"]["passed"]
        and not would_pass
        and study.store.active_library() is None,
        "development gate must independently reject",
    )
    routes = route_receipts(root, p)
    all_ids = {r["run"] for r in routes} | {f.stem for f in (root / "slots").glob("*.json")}
    start = read(root / "start.json")["cursor"]
    held = {t for group in p["holdout"].values() for t in group}
    with study.ledger.connect() as db:
        require(
            {r[0] for r in db.execute("SELECT DISTINCT run_id FROM calls WHERE rowid>?", (start,))} == all_ids
            and not held.intersection(r[0] for r in db.execute("SELECT task_id FROM test_exposures")),
            "foreign charges or test exposure",
        )
    with study.store.connect() as db:
        require(
            not held.intersection(r[0] for r in db.execute("SELECT task_id FROM runs WHERE split='test'")),
            "unexpected test run",
        )
    freeze = read(root / "route-freeze.json")
    lib = study.store.artifact(
        "skill_library",
        dict(
            schema=2,
            kind="skill_library",
            mode="live",
            members=POOLS["portfolio"],
            route_context="public_shape",
        ),
    )
    report = dict(
        schema=5,
        kind="library_evaluation",
        mode="live",
        baseline_library=None,
        baseline_members=[],
        members=POOLS["portfolio"],
        candidate=CHILD,
        library_id=lib,
        protocol=study.protocol,
        route_freeze=digest(freeze),
        observations=digest(rows),
        decisions=comps,
        interruption=digest(incident),
        gate={
            "policy": "matched_majority_portfolio_v1",
            "passed": False,
            "original_support_complete": False,
            "ungraded_originals": 3,
            "descriptive_replays": 3,
            "would_pass_with_replays": would_pass,
        },
    )
    receipt = study.store.artifact("library_evaluation", report)
    with study.store.connect(write=True) as db:
        db.execute(
            "INSERT OR IGNORE INTO library_evaluations VALUES(?,?,?,?,?)",
            (receipt, lib, None, canonical(report), 0),
        )
    gate = {**report, "receipt": receipt}
    atomic_trial(root / "gate.json", gate)
    result = dict(
        protocol=study.protocol,
        completed=True,
        original_support_complete=False,
        freeze=freeze,
        dev=dev,
        support=support,
        published=False,
        holdout=[],
        holdout_trials=[],
        curve=[],
        phase="gate_rejected",
        gate=gate,
        holdout_exposures=0,
        interruption=incident,
        costs=study.ledger.costs(sorted(all_ids)),
        transport=[read(f) for f in sorted((root / "transport").glob("*.json"))],
    )
    dump(root / "result.json", result)
    return result


if __name__ == "__main__":
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("command", choices=["freeze", "finish"])
    args = cli.parse_args()
    cfg = config(Path(".env.glm"))
    with session(ROOT, SEED, cfg) as (p, ledger, store):
        if args.command == "freeze":
            incident = record(ROOT, p, store, ledger)
            atomic_trial(ROOT / "interruption.json", incident)
            atomic_trial(INCIDENT, incident)
            print("Frozen interruption", digest(incident))
        else:
            r = recover(RecoveryStudy(ROOT, cfg, store, ledger, p), p, read(INCIDENT))
            print(r["phase"], r["costs"])
