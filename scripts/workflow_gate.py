"""Prospective, replayable release decision for a revised object specialization."""

import argparse
import itertools
import random
from collections import Counter, defaultdict
from pathlib import Path

from callback_policy import block_test
from evocoder.comparison import frozen_knowledge
from evocoder.contracts import canonical, digest
from evocoder.jobs import bounded_map
from evocoder.library import SkillLibrary
from falsy_transfer_protocol import FAMILIES as HOLDOUT_STRATA
from portfolio_routing import CHILD, PARENT
from report_click_procedure import read
from run_falsy_transfer import atomic_trial, require
from run_optional_control import curve_point
from workflow_learning import missed
from workflow_study import ROOT, WORKERS, session

LABEL = "object-label-policy"
PRINT = "transfer-print-ticket"
SCHEDULER = "learn-scheduler-watch"
FREEZE = Path("benchmarks/falsy_transfer/workflow-panel.json")


def arms_for(task, counts, targets, config, mode):
    if mode == "protection":
        return ("previous", "revised")
    arms = ("bare", "parent", "revised")
    if task in [*targets, *config, SCHEDULER]:
        arms += ("previous",)
    require(task in counts, "unregistered task")
    return arms


def plan(p, child, selected, support):
    targets = [LABEL, selected] if selected else []
    mode = "gate" if selected and child else ("protection" if child else "no_candidate")
    counts = {}
    if mode == "gate":
        counts.update(dict.fromkeys(sorted(support), 3))
        counts.update(dict.fromkeys([*p["old_objects"], selected], 5))
    if child:
        counts.update(dict.fromkeys(p["config"], 7))
        counts[SCHEDULER] = 5
    return dict(
        mode=mode,
        child=child,
        targets=targets,
        counts=counts,
        config=p["config"],
        arms={t: list(arms_for(t, counts, targets, p["config"], mode)) for t in counts},
    )


def checked_screen(s):
    result = read(ROOT / "screen.json")
    require(
        result["protocol"] == s.protocol and result["learning"] == digest(read(ROOT / "learning.json")),
        "screen anchor",
    )
    winner = None
    for index, family in enumerate(result["families"]):
        pair = s.p["sources"][index]
        rows = [read(ROOT / "rows" / (r + ".json")) for r in family["runs"]]
        require(
            not winner
            and pair == family["implementations"]
            and {(r["intent"]["task"], r["intent"]["repeat"]) for r in rows}
            == set(itertools.product(pair, range(5)))
            and len(rows) == 10
            and all(r["intent"]["phase"] == "screen" and r["intent"]["pool"] == [PARENT] for r in rows),
            "pilot changed or sampled adaptively",
        )
        counts = {t: sum(missed(r) for r in rows if r["intent"]["task"] == t) for t in pair}
        eligible = all(n >= 3 for n in counts.values()) and all(r["passed"] is not None for r in rows)
        require(family["misses"] == counts and family["eligible"] == eligible, "pilot eligibility differs")
        if eligible:
            winner = pair[1]
    require(
        result["selected"] == winner and (winner or len(result["families"]) == len(s.p["sources"])),
        "screen incomplete",
    )
    return result


def commit(s):
    learn = read(ROOT / "learning.json")
    screen = checked_screen(s)
    child = learn["proposal"].get("child")
    support = {}
    if child:
        lib = SkillLibrary(s.cfg, s.store, runner=object())
        require(
            lib.retain_parent(sorted([PARENT, CHILD]), child) == sorted([PARENT, child]), "wrong root/tip"
        )
        proof = lib.provenance(child)
        support = proof["support"]
        require(
            set(s.p["support"]) <= set(support)
            and all(digest(s.task(t).model_dump()) == k for t, k in support.items())
            and not proof["groups"].intersection(
                s.task(t).source_group
                for t in [
                    *s.p["old_objects"],
                    *s.p["config"],
                    *([screen["selected"]] if screen["selected"] else []),
                ]
            ),
            "support missing or sources overlap",
        )
    panel = dict(
        protocol=s.protocol,
        learning=digest(learn),
        screening=digest(screen),
        evaluator=digest(Path(__file__).read_text()),
        support=support,
        knowledge=frozen_knowledge(s.store),
        **plan(s.p, child, screen["selected"], support),
    )
    if not FREEZE.exists():
        require(
            not any(
                read(x)["phase"] in ("gate", "protection", "holdout")
                for x in (ROOT / "intents").glob("*.json")
            ),
            "evaluation already started",
        )
    atomic_trial(FREEZE, panel)
    atomic_trial(ROOT / "panel.json", panel)
    print("panel", panel["mode"], panel["targets"], len(support), digest(panel), flush=True)
    return panel


def registered(s):
    h = read(FREEZE)
    require(
        h == read(ROOT / "panel.json")
        and h["protocol"] == s.protocol
        and h["evaluator"] == digest(Path(__file__).read_text())
        and h["learning"] == digest(read(ROOT / "learning.json"))
        and h["screening"] == digest(checked_screen(s)),
        "panel or frozen decision changed",
    )
    return h


def schedule(p, h, n):
    tasks = [t for t, k in h["counts"].items() if n < k]
    rng = random.Random(digest([p["order_seed"], h["mode"], n]))
    rng.shuffle(tasks)
    result = []
    for t in tasks:
        arms = h["arms"][t]
        perms = list(itertools.permutations(arms))
        rng_task = random.Random(digest([p["order_seed"], t, n // len(perms)]))
        rng_task.shuffle(perms)
        result.extend((t, a) for a in perms[n % len(perms)])
    return result


def panel_round(s, h, n):
    require(h["child"] and 0 <= n < 7 and frozen_knowledge(s.store) == h["knowledge"], "candidate not frozen")
    if n:
        require((ROOT / f"panel-{n - 1}.json").is_file(), "previous round incomplete")
    jobs = schedule(s.p, h, n)
    rows = bounded_map(lambda j: s.solve(h["mode"], j[0], n, j[1], h["child"]), jobs, WORKERS)
    atomic_trial(ROOT / f"panel-{n}.json", dict(panel=digest(h), runs=[r["run"] for r in rows]))
    print("round", n + 1, len(rows), "graded", sum(r["passed"] is not None for r in rows), flush=True)


def direct_protection(rows, child):
    out = {}
    for t, names in [(PRINT, ("controls",)), (SCHEDULER, ("controls", "wake_replay"))]:
        group = [r for r in rows if r["task"] == t]
        counts = {}
        for a, skill in (("previous", CHILD), ("revised", child)):
            counts[a] = dict(
                total=len(group),
                graded=sum(r[a]["passed"] is not None for r in group),
                passed=sum(r[a]["passed"] is True for r in group),
                selected_specialization=sum(r[a]["selected"] == skill for r in group),
                probes={k: sum((r[a]["probes"] or {}).get(k) is True for r in group) for k in names},
            )
        old, new = counts["previous"], counts["revised"]
        probe_present = all(
            not r[a]["patch"] or all(k in (r[a]["probes"] or {}) for k in names)
            for r in group
            for a in ("previous", "revised")
        )
        complete = bool(group) and probe_present and all(c["graded"] == c["total"] for c in counts.values())
        passed = complete and all(new["probes"][k] >= old["probes"][k] for k in names)
        if t == PRINT:
            passed = passed and new["selected_specialization"] <= old["selected_specialization"]
        out[t] = dict(arms=counts, passed=passed, complete=complete)
    return out


def evaluate(p, h, rows):
    require(h["mode"] == "gate" and len(set(h["targets"])) == 2, "efficacy requires two eligible sources")
    decision = {}
    for base in ("parent", "bare"):
        tasks = {}
        for t, n in h["counts"].items():
            local = [r for r in rows if r["task"] == t]
            require(sorted(r["repeat"] for r in local) == list(range(n)), "missing registered comparison")
            c = Counter()
            for r in local:
                b, k = r[base], r["revised"]
                if b["passed"] is None or k["passed"] is None:
                    c["ungraded"] += 1
                    continue
                same = b["first_solver_request"] == k["first_solver_request"]
                require(
                    b["first_solver_request"]
                    and k["first_solver_request"]
                    and (not same or b["selected"] == k["selected"]),
                    "inconsistent first request",
                )
                diff = int(k["passed"]) - int(b["passed"])
                outcome = {-1: "loss", 0: "tie", 1: "win"}[diff]
                c[outcome] += 1
                c[("same_" if same else "changed_") + outcome] += 1
                c["attributed"] += int(diff > 0 and not same and k["selected"] == h["child"])
            cutoff = p["noise"]["raw_loss_veto"] if t in h["config"] else n // 2 + 1
            veto = c["changed_loss"] > c["changed_win"] or c["loss"] >= cutoff or bool(c["ungraded"])
            tasks[t] = dict(
                rounds=n,
                raw_loss_veto=cutoff,
                regressed=veto,
                **{
                    k: c[k]
                    for k in (
                        "win",
                        "loss",
                        "tie",
                        "changed_win",
                        "changed_loss",
                        "same_win",
                        "same_loss",
                        "attributed",
                        "ungraded",
                    )
                },
            )
        configs = [dict(r, portfolio=r["revised"]) for r in rows if r["task"] in h["config"]]
        complete = all(r[a]["passed"] is not None for r in configs for a in (base, "revised"))
        block = block_test(configs, base) if complete else {}
        veto = not complete or (
            sum(block["baseline_minus_candidate_by_round"]) > 0
            and block["one_sided_signflip_p"] <= p["noise"]["panel_signflip_alpha"]
        )
        wins = [t for t in h["targets"] if tasks[t]["attributed"] >= 3 and not tasks[t]["regressed"]]
        decision[base] = dict(
            tasks=tasks,
            targets_passing=wins,
            configuration_block=dict(block, veto=veto),
            passed=len(wins) == 2 and not veto and not any(x["regressed"] for x in tasks.values()),
        )
    direct = direct_protection(rows, h["child"])
    complete = all(r[a]["passed"] is not None for r in rows for a in h["arms"][r["task"]])
    passed = (
        complete and all(x["passed"] for x in decision.values()) and all(x["passed"] for x in direct.values())
    )
    return dict(comparisons=decision, direct=direct, complete=complete, passed=passed)


def observations(h):
    grouped = defaultdict(dict)
    for n in range(7):
        group = read(ROOT / f"panel-{n}.json")
        require(group["panel"] == digest(h), "foreign comparison batch")
        for rid in group["runs"]:
            row = read(ROOT / "rows" / (rid + ".json"))
            i = row["intent"]
            require(
                rid == digest(i)[:32]
                and i["protocol"] == h["protocol"]
                and i["phase"] == h["mode"]
                and i["repeat"] == n
                and n < h["counts"][i["task"]]
                and i["arm"] in h["arms"][i["task"]]
                and i["arm"] not in grouped[i["task"], n],
                "mixed or duplicate slot",
            )
            grouped[i["task"], n][i["arm"]] = row
    require(len(grouped) == sum(h["counts"].values()), "panel incomplete")
    for (t, _), r in grouped.items():
        require(set(r) == set(h["arms"][t]), "incomplete cell")
    return [dict(task=t, repeat=n, **r) for (t, n), r in sorted(grouped.items())]


def decide(s, h):
    rows = observations(h)
    if h["mode"] == "protection":
        outcome = direct_protection(rows, h["child"])
        atomic_trial(
            ROOT / "protection.json", dict(panel=digest(h), outcome=outcome, observations=digest(rows))
        )
        print("direct protection only", outcome, flush=True)
        return
    require(h["mode"] == "gate", "candidate not eligible")
    verdict = evaluate(s.p, h, rows)
    members = sorted([PARENT, h["child"]])
    target = s.store.artifact(
        "skill_library",
        dict(schema=2, kind="skill_library", mode="live", members=members, route_context="public_shape"),
    )
    record = dict(
        schema=2,
        kind="library_evaluation",
        mode="live",
        protocol=s.protocol,
        panel=digest(h),
        candidate=h["child"],
        baseline_library=None,
        baseline_members=[],
        members=members,
        library_id=target,
        observations=digest(rows),
        gate=verdict,
    )
    key = s.store.artifact("library_evaluation", record)
    with s.store.connect(write=True) as db:
        db.execute(
            "INSERT OR IGNORE INTO library_evaluations VALUES(?,?,?,?,?)",
            (key, target, None, canonical(record), int(verdict["passed"])),
        )
        row = db.execute("SELECT report,passed FROM library_evaluations WHERE id=?", (key,)).fetchone()
        require(tuple(row) == (canonical(record), int(verdict["passed"])), "database gate differs")
    atomic_trial(ROOT / "gate.json", dict(receipt=key, report=record))
    if verdict["passed"]:
        if s.store.active_library() is None:
            require(frozen_knowledge(s.store) == h["knowledge"], "knowledge diverged before activation")
            s.store.activate_library(target, key, expected=None, mode="live")
        with s.store.connect() as db:
            tx = dict(
                db.execute(
                    "SELECT seq,before_id,after_id,evaluation_id FROM library_releases WHERE after_id=? AND evaluation_id=?",
                    (target, key),
                ).fetchone()
            )
        require(tx["before_id"] is None, "wrong transition")
        atomic_trial(
            ROOT / "activation.json",
            dict(
                protocol=s.protocol,
                library=target,
                members=members,
                receipt=key,
                transaction=tx,
                knowledge=frozen_knowledge(s.store),
            ),
        )
        s.activation()
    else:
        require(s.store.active_library() is None, "rejected library active")
    print("release gate", verdict["passed"], key, flush=True)


def holdout(s, h):
    s.activation()
    pairs = []
    curve = []
    for family in HOLDOUT_STRATA:
        tasks = s.p["holdout"][family]
        jobs = [
            (t, a)
            for i, t in enumerate(tasks)
            for a in (("revised", "bare") if i % 2 else ("bare", "revised"))
        ]
        data = bounded_map(lambda j: s.solve("holdout", j[0], 0, j[1], h["child"]), jobs, WORKERS)
        local = []
        for t in tasks:
            by_arm = {r["intent"]["arm"]: r for r in data if r["intent"]["task"] == t}
            local.append(
                dict(
                    task=t,
                    family=family,
                    baseline_run=by_arm["bare"]["run"],
                    candidate_run=by_arm["revised"]["run"],
                    baseline_pass=by_arm["bare"]["passed"],
                    candidate_pass=by_arm["revised"]["passed"],
                )
            )
        pairs.extend(local)
        if all(x[k] is not None for x in pairs for k in ("baseline_pass", "candidate_pass")):
            curve.append(curve_point(pairs, family, local))
    s.activation()
    require(len({x["task"] for x in pairs}) == 30, "wrong holdout")
    with s.ledger.connect() as db:
        claims = {
            r["task_id"]: r["task_hash"]
            for r in db.execute("SELECT * FROM test_exposures WHERE experiment=?", (s.protocol,))
        }
    require(claims == s.p["holdout_hashes"], "incorrect holdout claims")
    atomic_trial(
        ROOT / "holdout.json", dict(panel=digest(h), pairs=pairs, curve=curve if len(curve) == 3 else [])
    )


if __name__ == "__main__":
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("command", choices=["freeze", "round", "decide", "holdout"])
    cli.add_argument("--round", type=int, choices=range(1, 8))
    args = cli.parse_args()
    with session() as study:
        if args.command == "freeze":
            commit(study)
        else:
            registration = registered(study)
            if args.command == "round":
                require(args.round is not None, "supply a round")
                panel_round(study, registration, args.round - 1)
            elif args.command == "decide":
                decide(study, registration)
            else:
                holdout(study, registration)
