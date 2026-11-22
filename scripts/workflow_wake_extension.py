"""Prospective learning-only follow-up for a missing default after a cross-worker wake."""

from pathlib import Path

from evocoder.contracts import digest
from evocoder.jobs import bounded_map
from portfolio_routing import CHILD
from report_click_procedure import read
from run_falsy_transfer import atomic_trial, require
from workflow_learning import proposal
from workflow_study import FLAGS, LEARN, PUBLIC, ROOT, WORKERS, session

ADDENDUM = PUBLIC.with_name("workflow-wake-replay.json")
FOLLOWUP = ROOT / "wake-learning.json"


def registration():
    p = read(PUBLIC)
    return dict(
        study="prospective-default-on-wake-v1",
        base=digest(p),
        code=digest(Path(__file__).read_text()),
        condition="Only if the original calibration finishes without an accepted revision, before any candidate development solve.",
        reason="Exploratory learning repeatedly exposed wrong default selection after moving a ticket to a different "
        "worker. The original strict witness specifically required a failed supplied-object resume. "
        "Keep the initial outcome and test the independently declared omitted-object contract in four NEW rounds.",
        tasks={t: p["programs"][t]["snapshot"] for t in LEARN},
        normal_rounds=4,
        fixed_tasks=["harbor-slots"],
        extra_fixed_rounds=2,
        witnesses="Fresh learner runs only. Require a pure-boolean misroute under the normal old portfolio, prefer a "
        "submitted failure then earliest natural exposure. Require the old object child actually applied, public tests "
        "passed, same submitted patch failed the public-contract exercise, and the harbor absent_after_move or a "
        "supplied-object resumed probe failed. Prefer natural submitted failures. Include up to two same-task "
        "successful contrasts and one observation per other learning task, six maximum. No evaluator artifacts "
        "or hidden probes enter the proposal request. The old registration, grades and candidate remain immutable.",
        downstream="If this independently registered learning stage produces a revision, bind it to a new prospective "
        "panel receipt before any candidate development solve. Preserve the two-implementation source requirement, "
        "five-round efficacy, seven-round configuration protection, ancestry and sealed holdout protocol.",
    )


def choose(rows):
    flags = [
        r
        for r in rows
        if r["intent"]["task"] in FLAGS
        and r["intent"]["arm"] == "previous"
        and r["selected"] == CHILD
        and r["patch"]
        and r["passed"] is not None
    ]
    wake = [
        r
        for r in rows
        if r["intent"]["task"] not in FLAGS
        and r["selected"] == CHILD
        and r["patch"]
        and r["passed"] is False
        and r["probes"]["public"]
        and r["public_feedback"]["passed"] is False
        and (r["probes"].get("absent_after_move") is False or r["probes"].get("resumed") is False)
    ]
    flags.sort(key=lambda r: r["passed"])
    wake.sort(key=lambda r: r["intent"]["arm"] != "previous")
    if not flags or not wake:
        return [], flags, wake
    first = [flags[0], wake[0]]
    selected = first.copy()
    for r in first:
        other = next(
            (
                x
                for x in rows
                if x["intent"]["task"] == r["intent"]["task"]
                and x["run"] != r["run"]
                and x["passed"]
                and x["public_feedback"]["passed"]
                and (r["passed"] is False or x["selected"] != r["selected"])
            ),
            None,
        )
        if other:
            selected.append(other)
    for t in LEARN:
        if t not in {x["intent"]["task"] for x in selected}:
            x = next(
                (x for x in rows if x["intent"]["task"] == t and x["patch"] and x["public_feedback"]), None
            )
            if x:
                selected.append(x)
    return selected[:6], flags, wake


def effective_learning():
    initial = read(ROOT / "learning.json")
    if initial["proposal"]["child"]:
        return initial
    if FOLLOWUP.exists():
        r = read(FOLLOWUP)
        require(
            r["initial"] == digest(initial)
            and r["addendum"] == digest(registration())
            and read(ADDENDUM) == registration(),
            "different supplementary learning",
        )
        return r
    return initial


def run(s):
    initial = read(ROOT / "learning.json")
    require(
        not initial["proposal"]["child"]
        and not (ROOT / "panel.json").exists()
        and read(ADDENDUM) == registration(),
        "follow-up must precede candidate evaluation and be prospectively registered",
    )
    if FOLLOWUP.exists():
        return effective_learning()
    original = s.protocol
    token = dict(initial=digest(initial), addendum=digest(registration()))
    s.protocol = digest(token)
    try:
        jobs = [
            (t, n, a)
            for n in range(4)
            for t in LEARN
            for a in (("previous", "fixed") if t == "harbor-slots" and n < 2 else ("previous",))
        ]
        rows = bounded_map(lambda j: s.solve("learn", *j), jobs, WORKERS)
        chosen, flags, wake = choose(rows)
        rev = (
            proposal(s, chosen)
            if chosen
            else dict(child=None, refs=[], attempts=[], reason="no prospective wake/flag contrast")
        )
        result = dict(
            protocol=s.protocol,
            **token,
            runs=[r["run"] for r in rows],
            flag_misroutes=[r["run"] for r in flags],
            wake_failures=[r["run"] for r in wake],
            proposal=rev,
        )
        atomic_trial(FOLLOWUP, result)
        print("follow-up", len(rows), len(flags), len(wake), rev, flush=True)
        return result
    finally:
        s.protocol = original


if __name__ == "__main__":
    import sys

    if sys.argv[1:] == ["register"]:
        require(not FOLLOWUP.exists() and not (ROOT / "panel.json").exists(), "already collected")
        atomic_trial(ADDENDUM, registration())
        print("registered", digest(registration()))
    elif sys.argv[1:] == ["run"]:
        with session() as study:
            run(study)
    else:
        raise SystemExit("use register or run")
