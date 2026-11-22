"""Frozen pilot, repeated repair and configuration noise rules for the callback release study."""

from collections import Counter
from itertools import product
from math import comb

from evocoder.contracts import digest
from run_falsy_transfer import require
from run_shape_routing import pair


def binomial_tail(n, k, p):
    return sum(comb(n, i) * p**i * (1 - p) ** (n - i) for i in range(k, n + 1))


def calibrate_noise(prior, config_ids):
    observations = []
    for r in prior["dev"]:
        if r["task"] in config_ids:
            a, b = r["parent"], r["issue_portfolio"]
            require(
                a["first_solver_request"] == b["first_solver_request"]
                and a["selected"] == b["selected"]
                and a["run"] != b["run"],
                "historical A/A pair differs at solver input",
            )
            observations.append(
                [
                    r["task"],
                    r["repeat"],
                    a["run"],
                    b["run"],
                    a["first_solver_request"],
                    a["passed"],
                    b["passed"],
                ]
            )
    n = len(observations)
    require(n == 20 and len({(r[0], r[1]) for r in observations}) == 20, "incomplete historical null panel")
    discordant = sum(a != b for *_, a, b in observations)
    # One-sided exact 95% upper bound on discordance; randomized arm order supplies sign symmetry.
    lo, hi = 0.0, 1.0
    for _ in range(64):
        mid = (lo + hi) / 2
        if 1 - binomial_tail(n, discordant + 1, mid) > 0.05:
            lo = mid
        else:
            hi = mid
    per_pair = (lo + hi) / 4
    limit = 0.025 / (len(config_ids) * 2)
    k = next(i for i in range(4, 8) if binomial_tail(7, i, per_pair) <= limit)
    return dict(
        null_pairs=n,
        discordant=discordant,
        observations=digest(observations),
        discordance_upper_95=round(per_pair * 2, 9),
        loss_probability_bound=round(per_pair, 9),
        raw_loss_veto=k,
        per_comparison_tail_limit=limit,
        tail_at_veto=round(binomial_tail(7, k, per_pair), 9),
        panel_signflip_alpha=0.025 / 2,
    )


def pilot_summary(p, rows):
    expected = [(n, m["pilot"]["id"]) for n in range(5) for m in p["families"].values()]
    require([(r["repeat"], r["task"]) for r in rows] == expected, "missing or reordered pilot runs")
    result = {}
    seen = set()
    for family, parts in p["families"].items():
        meta = parts["pilot"]
        local = [r for r in rows if r["task"] == meta["id"]]
        bad = []
        for r in local:
            a = r["parent"]
            require(
                a["run"] not in seen
                and r["snapshot"] == meta["snapshot"]
                and r["split"] == "learn"
                and r["order"] == ["parent"],
                "unbound pilot arm",
            )
            seen.add(a["run"])
            probes = a["probes"] or {}
            if (
                not a["passed"]
                and a["patch"]
                and probes.get("public")
                and probes.get("controls")
                and any(not ok for k, ok in probes.items() if k not in {"public", "controls"})
            ):
                bad.append(r["repeat"] + 1)
        result[family] = dict(
            qualifying_failure_rounds=bad,
            raw_failures=sum(not r["parent"]["passed"] for r in local),
            eligible=len(bad) >= 3,
            confirm=parts["confirmation"]["id"],
        )
    return {"protocol": digest(p), "observations": digest(rows), "sources": result}


def block_test(rows, base):
    differences = [
        sum(int(r[base]["passed"]) - int(r["portfolio"]["passed"]) for r in rows if r["repeat"] == n)
        for n in range(7)
    ]
    actual = sum(differences)
    values = [sum(x * y for x, y in zip(sign, differences)) for sign in product((-1, 1), repeat=7)]
    return {
        "baseline_minus_candidate_by_round": differences,
        "one_sided_signflip_p": sum(x >= actual for x in values) / len(values),
    }


def comparisons(p, pilot, rows):
    groups = {
        **p["protected"],
        **{v["confirmation"]["id"]: v["confirmation"] for v in p["families"].values()},
    }
    eligible = {v["confirm"] for v in pilot["sources"].values() if v["eligible"]}
    objects = set(p["objects"]) | {v["confirm"] for v in pilot["sources"].values()}
    config = set(p["config"])
    seen = set()
    for r in rows:
        m = groups[r["task"]]
        require(
            (r["snapshot"], r["group"], r["split"]) == (m["snapshot"], m["group"], m["split"]),
            "different frozen repair",
        )
        for a in ("bare", "parent", "portfolio"):
            key = r[a]["run"]
            require(
                key not in seen
                and r[a]["image"] == p["image"]
                and isinstance(r[a]["passed"], bool)
                and r[a]["state"] in ("COMPLETE", "FAILED", "EXHAUSTED"),
                "reused or incomplete solve",
            )
            seen.add(key)
    require(set(groups) == {r["task"] for r in rows}, "incomplete release panel")
    outcomes = {}
    for base in ("parent", "bare"):
        details, confirmed, regressions = {}, set(), []
        for tid, m in groups.items():
            local = sorted((r for r in rows if r["task"] == tid), key=lambda x: x["repeat"])
            n = 7 if tid in config else (5 if tid in objects else 3)
            require(
                [r["repeat"] for r in local] == list(range(n)), "missing or duplicate registered repetitions"
            )
            c = Counter()
            for r in local:
                x = pair(r, base)
                same = x["baseline_request"] == x["candidate_request"]
                require(
                    not x["infrastructure_error"]
                    and (not same or x["baseline_applied_skill"] == x["candidate_applied_skill"]),
                    "unsettled infrastructure or incompatible wire identity",
                )
                outcome = (
                    "win"
                    if x["candidate_pass"] > x["baseline_pass"]
                    else "loss"
                    if x["candidate_pass"] < x["baseline_pass"]
                    else "tie"
                )
                c[outcome] += 1
                c[("same_" if same else "changed_") + outcome] += 1
                if not same and outcome == "win" and x["skill_behavior"] == "candidate_applied":
                    c["attributed"] += 1
            cutoff = p["noise"]["raw_loss_veto"] if tid in config else n // 2 + 1
            bad = c["changed_loss"] > c["changed_win"] or c["loss"] >= cutoff
            if bad:
                regressions.append(tid)
            countable = tid in objects and (tid in p["objects"] or tid in eligible)
            if countable and c["attributed"] >= 3 and not bad:
                confirmed.add(m["group"])
            details[tid] = {
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
                )
            }
            details[tid].update(raw_loss_veto=cutoff, regressed=bad, rounds=n, counts_for_efficacy=countable)
        block = block_test([r for r in rows if r["task"] in config], base)
        block["veto"] = (
            block["one_sided_signflip_p"] <= p["noise"]["panel_signflip_alpha"]
            and sum(block["baseline_minus_candidate_by_round"]) > 0
        )
        fresh = sorted({groups[t]["group"] for t in eligible} & confirmed)
        outcomes[base] = {
            "tasks": details,
            "confirmed_object_sources": sorted(confirmed),
            "confirmed_new_sources": fresh,
            "regressions": sorted(regressions),
            "raw_config_panel": block,
            "passed": len(confirmed) >= 2 and bool(fresh) and not regressions and not block["veto"],
        }
    return outcomes, all(v["passed"] for v in outcomes.values())
