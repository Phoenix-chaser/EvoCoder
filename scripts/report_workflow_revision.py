"""Export privacy-scoped GLM receipts and independently recompute the workflow decision offline."""

import argparse
from collections import Counter, defaultdict
from decimal import Decimal
from pathlib import Path

from callback_protocol import PUBLIC as PRIOR_REGISTRATION
from report_callback_release import workspaces as prior_workspaces
from workflow_cases import cases

from evocoder.comparison import dump
from evocoder.contracts import digest
from evocoder.grounding import validate_summary
from portfolio_routing import CHILD, PARENT
from report_click_procedure import read
from run_falsy_transfer import require
from run_optional_control import curve_point
from workflow_candidate_selection import CHOICE, RULE, choose as choose_candidate, score
from workflow_gate import FREEZE, direct_protection, evaluate, observations, plan, registered
from workflow_learning import missed, witnesses
from workflow_study import FAMILIES, FLAGS, PUBLIC, RESULT, ROOT, WAKES, session, sources_hash
from workflow_wake_extension import ADDENDUM, FOLLOWUP, choose


def panel_rows(rows, h):
    grouped = defaultdict(dict)
    for r in rows:
        i = r["intent"]
        if i["phase"] not in ("gate", "protection"):
            continue
        require(
            i["phase"] == h["mode"]
            and i["task"] in h["counts"]
            and 0 <= i["repeat"] < h["counts"][i["task"]]
            and i["arm"] in h["arms"][i["task"]],
            "unregistered comparison",
        )
        cell = grouped[i["task"], i["repeat"]]
        require(i["arm"] not in cell, "duplicate comparison")
        cell[i["arm"]] = r
    expected = {(t, n) for t, k in h["counts"].items() for n in range(k)}
    require(
        set(grouped) == expected and all(set(r) == set(h["arms"][t]) for (t, _), r in grouped.items()),
        "partial panel",
    )
    return [dict(task=t, repeat=n, **r) for (t, n), r in sorted(grouped.items())]


def add_cost(costs):
    return {
        k: (sum(r[k] for r in costs) if k == "calls" else float(sum(Decimal(str(r[k])) for r in costs)))
        for k in ("calls", "cost_credits", "reserved_credits")
    }


def review(data, p, h):
    require(
        data["protocol"] == digest(p) == h["protocol"]
        and data["panel"] == digest(h)
        and data["model"] == p["model"],
        "incorrect study archive",
    )
    require(
        digest(p) == "aacf076a8acc7009c661e87080779875e0c5b11c499d9c9ec78bcc05065aa188"
        and p["code"] == sources_hash()
        and h["evaluator"] == digest(Path(__file__).with_name("workflow_gate.py").read_text()),
        "changed registered protocol implementation",
    )
    old = read(PRIOR_REGISTRATION)
    known = prior_workspaces(old, bool(data["activation"]))
    for t, ref, program in cases().values():
        m = p["programs"][t.id]
        require(
            digest(ref) == m["reference"] and (not program or digest(program) == m["feedback"]),
            "different synthetic reference",
        )
        known[t.id] = t
    hashes = {tid: digest(t.model_dump()) for tid, t in known.items()}
    rows = data["rows"]
    index = {r["run"]: r for r in rows}
    require(len(index) == len(rows), "reused run")
    for r in rows:
        i = r["intent"]
        require(
            r["run"] == digest(i)[:32]
            and r["solver_calls"] <= p["solver_actions"]
            and i["arm"] in ("previous", "fixed", "parent", "bare", "revised"),
            "arm receipt changed",
        )
        require(
            hashes.get(i["task"]) == i["snapshot"]
            and i["view"] == ("public_shape" if i["arm"] in ("previous", "revised") else "issue")
            and i["fixed"] == (i["arm"] == "fixed")
            and i["purpose"] == ("solve" if i["phase"] == "learn" else "evaluate"),
            "incorrect input snapshot, memory view or role",
        )
        if i["arm"] == "revised" and i["phase"] == "learn":
            require(
                len(i["pool"]) == 2 and PARENT in i["pool"] and i["pool"] == sorted(i["pool"]),
                "wrong learn treatment",
            )
        else:
            expected = {
                "parent": [PARENT],
                "fixed": [CHILD],
                "bare": [],
                "previous": sorted([PARENT, CHILD]),
                "revised": sorted([PARENT, h["child"]]) if h["child"] else [],
            }[i["arm"]]
            require(i["pool"] == expected, "changed experimental treatment")
        if r["passed"] is not None:
            require(
                r["state"] in ("COMPLETE", "FAILED", "EXHAUSTED")
                and r["first_solver_request"]
                and not r["cost"]["reserved_credits"],
                "unsettled paid grade",
            )
            if r["probes"] is not None:
                require(
                    bool(r["patch"]) and all(r["probes"].values()) == r["passed"], "inconsistent patch probes"
                )
        require(r["selected"] in [None, *i["pool"]], "route outside treatment")
    learning = data["learning"]
    selected = set()
    for j, stage in enumerate(learning):
        local = [index[k] for k in stage["runs"]]
        require(
            all(
                x["intent"]["phase"] == "learn"
                and x["intent"]["protocol"] == stage["protocol"]
                and x["intent"]["task"] in FLAGS + WAKES
                and x["intent"]["purpose"] == "solve"
                for x in local
            ),
            "proposal received development data",
        )
        if j == 0:
            flags, wake = witnesses(local)
        else:
            require(
                stage["initial"] == digest(learning[0])
                and stage["addendum"] == digest(read(ADDENDUM))
                and stage["protocol"] == digest({"initial": stage["initial"], "addendum": stage["addendum"]}),
                "follow-up preregistration mismatch",
            )
            _, flags, wake = choose(local)
        require(
            stage["flag_misroutes"] == [x["run"] for x in flags]
            and stage["wake_failures"] == [x["run"] for x in wake],
            "learning witnesses differ",
        )
        selected.update(stage["runs"])
    learn = learning[-1]
    prop = learn["proposal"]
    selection = data.get("selection")
    candidate = prop["child"]
    revision_sources = [(prop, set(learn["runs"]))]
    if selection:
        require(
            selection["learning"] == digest(learn)
            and selection["rule"] == digest(read(RULE))
            and selection["primary"] == candidate
            and h["selection"] == digest(selection),
            "different learning choice",
        )
        qualifier = selection.get("proposal")
        for label, ids, keys in (
            ("initial-scope-audit", selection["audit"], [candidate]),
            (
                "matched-learning",
                selection["matched"],
                [candidate, qualifier["child"]] if qualifier and qualifier.get("child") else [],
            ),
        ):
            records = [index[k] for k in ids]
            expected_stage = digest(dict(base=digest(p), selection=digest(read(RULE)), stage=label))
            expected = {(t, n, k) for t in FLAGS + WAKES for n in range(2) for k in keys}
            actual = {
                (
                    r["intent"]["task"],
                    r["intent"]["repeat"],
                    next(k for k in keys if k in r["intent"]["pool"]),
                )
                for r in records
            }
            require(
                actual == expected
                and len(records) == len(expected)
                and all(
                    r["intent"]["protocol"] == expected_stage
                    and r["intent"]["phase"] == "learn"
                    and r["intent"]["arm"] == "revised"
                    and r["intent"]["purpose"] == "solve"
                    for r in records
                ),
                "invalid matched learning assignments",
            )
            selected.update(ids)
        if qualifier:
            revision_sources.append((qualifier, set(learn["runs"] + selection["audit"])))
            if qualifier["child"]:
                scores = {
                    k: score([index[r] for r in selection["matched"] if k in index[r]["intent"]["pool"]], k)
                    for k in (candidate, qualifier["child"])
                }
                require(scores == selection["scores"], "wrong scores on matched learning runs")
                if choose_candidate(scores[candidate], scores[qualifier["child"]]):
                    candidate = qualifier["child"]
        require(candidate == selection["selected"], "different selected revision")
    require(h["learning"] == digest(learn) and h["child"] == candidate, "wrong candidate")
    for revision, permitted in revision_sources:
        if revision["child"]:
            receipt = data["revision_receipts"][revision["child"]]
            require(
                digest(receipt) == revision["receipt"]
                and receipt["candidate"] == revision["child"]
                and receipt["parent"] == CHILD
                and receipt["frozen_evidence"] == revision["evidence"]
                and set(revision["required"])
                <= set(receipt["evidence_refs"])
                <= set(revision["refs"])
                <= permitted
                and {"steps", "applicability"} <= set(receipt["changed_fields"]),
                "revision evidence mismatch",
            )
    screen = data["screen"]
    require(
        screen["protocol"] == digest(p)
        and screen["learning"] == digest(learning[0])
        and h["screening"] == digest(screen),
        "wrong parent screening",
    )
    winning = None
    for j, family in enumerate(screen["families"]):
        names = FAMILIES[j]
        attempts = [index[k] for k in family["runs"]]
        require(
            winning is None
            and list(names) == family["implementations"]
            and len(attempts) == 10
            and {(r["intent"]["task"], r["intent"]["repeat"]) for r in attempts}
            == {(t, n) for t in names for n in range(5)}
            and all(r["intent"]["phase"] == "screen" and r["intent"]["pool"] == [PARENT] for r in attempts),
            "screen was not parent-only or has incomplete arms",
        )
        counts = {t: sum(missed(r) for r in attempts if r["intent"]["task"] == t) for t in names}
        eligible = all(n >= 3 for n in counts.values()) and all(r["passed"] is not None for r in attempts)
        require(counts == family["misses"] and eligible == family["eligible"], "screen outcomes differ")
        if eligible:
            winning = names[1]
        selected.update(family["runs"])
    require(
        winning == screen["selected"] and (winning or len(screen["families"]) == len(FAMILIES)),
        "screen selection differs",
    )
    assignments = plan(p, candidate, winning, h["support"])
    require(all(h[k] == v for k, v in assignments.items()), "wrong formal assignment")
    panels = panel_rows(rows, h)
    selected.update(r[a]["run"] for r in panels for a in h["arms"][r["task"]])
    if h["mode"] == "gate":
        decision = evaluate(p, h, panels)
        formal = data["gate"]
        require(
            formal["report"]["gate"] == decision
            and formal["report"]["observations"] == digest(panels)
            and formal["receipt"] == digest(formal["report"])
            and formal["report"]["candidate"] == h["child"]
            and formal["report"]["members"] == sorted([PARENT, h["child"]])
            and formal["report"]["library_id"]
            == digest(
                dict(
                    schema=2,
                    kind="skill_library",
                    mode="live",
                    members=sorted([PARENT, h["child"]]),
                    route_context="public_shape",
                )
            ),
            "incorrect gate",
        )
        allow = decision["passed"]
    else:
        require(data["gate"] is None, "ineligible experiment appears released")
        if h["mode"] == "protection":
            require(data["protection"] == direct_protection(panels, h["child"]), "protection differs")
        allow = False
    exposures = data["holdout_exposures"]
    require(bool(data["activation"]) == allow, "activation mismatch")
    if allow:
        a = data["activation"]
        formal = data["gate"]
        require(
            a["library"] == formal["report"]["library_id"] == a["transaction"]["after_id"]
            and a["receipt"] == a["transaction"]["evaluation_id"] == formal["receipt"]
            and a["transaction"]["before_id"] is None
            and exposures == 30,
            "published library does not match evaluator",
        )
        local = data["holdout"]["pairs"]
        require(
            {x["task"] for x in local} == set(p["holdout_hashes"]) and len(local) == 30, "wrong test catalog"
        )
        cumulative = []
        points = []
        for fam, ids in p["holdout"].items():
            family = [r for r in local if r["family"] == fam]
            require([r["task"] for r in family] == ids, "changed test assignments")
            for pair in family:
                for kind, arm in (("baseline", "bare"), ("candidate", "revised")):
                    rid = pair[kind + "_run"]
                    r = index[rid]
                    i = r["intent"]
                    require(
                        i["phase"] == "holdout"
                        and i["arm"] == arm
                        and i["task"] == pair["task"]
                        and i["snapshot"] == p["holdout_hashes"][i["task"]]
                        and i["repeat"] == 0
                        and i["library"] == (a["library"] if arm == "revised" else None)
                        and pair[kind + "_pass"] == r["passed"],
                        "wrong held-out observation",
                    )
                    selected.add(rid)
            cumulative.extend(family)
            if all(r[k] is not None for r in cumulative for k in ("baseline_pass", "candidate_pass")):
                points.append(curve_point(cumulative, fam, family))
        require(
            data["holdout"]["curve"] == (points if len(points) == 3 else []), "incorrect diversity points"
        )
    else:
        require(exposures == 0 and data["holdout"] is None, "unpublished skill exposed to held-out programs")
    require(selected == set(index), "unassigned paid attempt")
    calls = [r["cost"] for r in rows] + list(data["proposal_billing"].values())
    require(
        set(data["proposal_billing"]) == {a for revision, _ in revision_sources for a in revision["attempts"]}
        and add_cost(calls) == data["billing"],
        "incorrect accounting",
    )
    summary = {}
    for phase in ("learn", "screen", "protection", "gate", "holdout"):
        counts = Counter(r["passed"] for r in rows if r["intent"]["phase"] == phase)
        if counts:
            summary[phase] = dict(
                solves=sum(counts.values()), passed=counts[True], failed=counts[False], ungraded=counts[None]
            )
    return dict(phases=summary, mode=h["mode"], published=allow, exposures=exposures, billing=data["billing"])


def export(s):
    h = registered(s)
    stages = [read(ROOT / "learning.json")]
    if FOLLOWUP.exists():
        stages.append(read(FOLLOWUP))
    screen = read(ROOT / "screen.json")
    rows = sorted((read(x) for x in (ROOT / "rows").glob("*.json")), key=lambda r: r["run"])
    proposals = [rid for stage in stages for rid in stage["proposal"]["attempts"]]
    choice = read(CHOICE) if CHOICE.exists() else None
    revisions = [s["proposal"] for s in stages]
    if choice and choice["proposal"]:
        revisions.append(choice["proposal"])
        proposals.extend(choice["proposal"]["attempts"])
    gate = None
    direct = None
    if h["mode"] == "gate":
        gate = read(ROOT / "gate.json")
        with s.store.connect() as db:
            sql = db.execute(
                "SELECT report,passed FROM library_evaluations WHERE id=?", (gate["receipt"],)
            ).fetchone()
        from evocoder.contracts import canonical

        require(
            tuple(sql) == (canonical(gate["report"]), int(gate["report"]["gate"]["passed"])),
            "wrong SQL evaluation",
        )
    if h["mode"] == "protection":
        actual = read(ROOT / "protection.json")
        require(actual["observations"] == digest(observations(h)), "changed protection collection")
        direct = actual["outcome"]
    ids = {r["run"] for r in rows} | set(proposals)
    cursor = read(ROOT / "start.json")["cursor"]
    with s.ledger.connect() as db:
        recent = list(
            db.execute("SELECT rowid,id,run_id,state FROM calls WHERE rowid>? ORDER BY rowid", (cursor,))
        )
        exposures = {r[0] for r in db.execute("SELECT task_id FROM test_exposures")} & set(
            s.p["holdout_hashes"]
        )
    records = defaultdict(list)
    for r in recent:
        if r["run_id"] in ids:
            records[r["run_id"]].append(r)
    require(set(records) == ids, "some study runs do not own paid calls")
    for rid, calls in records.items():
        events = s.store.events(rid)
        settled = {r["id"] for r in calls if r["state"] == "settled"}
        returned = {e["payload"]["call_id"] for e in events if e["kind"] == "MODEL_RESPONSE"}
        require(
            returned == settled
            and all(
                e["payload"]["provider_model"].lower() == s.cfg.model.lower()
                for e in events
                if e["kind"] == "MODEL_RESPONSE"
            ),
            "ledger and real GLM response differ",
        )
    receipts = {}
    for prop in revisions:
        if prop["child"]:
            skill = s.store.get_artifact(prop["child"])
            receipt = s.store.get_artifact(prop["receipt"])
            validate_summary(
                skill["change_summary"], skill["evidence_refs"], s.store.get_artifact(receipt["catalog"])
            )
            receipts[prop["child"]] = receipt
    activation = s.activation() if (ROOT / "activation.json").exists() else None
    require((s.store.active_library() is None) == (activation is None), "unexpected activation")
    d = dict(
        schema=1,
        protocol=s.protocol,
        panel=digest(h),
        model=s.cfg.model,
        rows=rows,
        learning=stages,
        screen=screen,
        selection=choice,
        revision_receipts=receipts,
        gate=gate,
        protection=direct,
        activation=activation,
        holdout=read(ROOT / "holdout.json") if (ROOT / "holdout.json").exists() else None,
        holdout_exposures=len(exposures),
        proposal_billing={rid: s.store.costs([rid]) for rid in proposals},
        billing=s.store.costs(sorted(ids)),
    )
    d["summary"] = review(d, s.p, h)
    dump(RESULT, d)
    return d["summary"]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["export", "verify"])
    args = parser.parse_args()
    if args.command == "export":
        with session() as study:
            print(export(study))
    else:
        data = read(RESULT)
        actual = review(data, read(PUBLIC), read(FREEZE))
        require(actual == data["summary"], "different summary")
        print(actual)
