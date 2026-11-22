"""Export privacy-scoped GLM receipts and independently recompute the workflow decision offline."""

import argparse
from collections import Counter, defaultdict
from decimal import Decimal
from pathlib import Path

from callback_protocol import PUBLIC as PRIOR_REGISTRATION
from report_callback_release import workspaces as prior_workspaces
from workflow_cases import cases

from evocoder.comparison import dump
from evocoder.contracts import Settings, digest
from evocoder.grounding import validate_summary
from evocoder.jobs import bounded_map
from evocoder.runtime import grade, verify_public
from falsy_transfer_protocol import probe_patch
from portfolio_routing import CHILD, PARENT
from report_click_procedure import read
from run_falsy_transfer import require
from run_optional_control import curve_point
from workflow_candidate_selection import CHOICE, RULE, choose as choose_candidate, score
from workflow_gate import FREEZE, direct_protection, evaluate, observations, plan, registered
from workflow_learning import missed, witnesses
from workflow_study import (
    FAMILIES,
    FLAGS,
    PUBLIC,
    RESULT,
    ROOT,
    WAKES,
    WORKERS,
    public_check,
    session,
    sources_hash,
)
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


def revision_comparison(panels, child):
    results = {}
    for tid in sorted({r["task"] for r in panels if "previous" in r}):
        local = [r for r in panels if r["task"] == tid]
        paired = [r for r in local if all(r[a]["passed"] is not None for a in ("previous", "revised"))]
        results[tid] = dict(
            planned=len(local),
            graded_pairs=len(paired),
            previous_pass=sum(r["previous"]["passed"] for r in paired),
            revised_pass=sum(r["revised"]["passed"] for r in paired),
            revised_only=sum(r["revised"]["passed"] and not r["previous"]["passed"] for r in paired),
            previous_only=sum(r["previous"]["passed"] and not r["revised"]["passed"] for r in paired),
            selected_old=sum(r["previous"]["selected"] == CHILD for r in local),
            selected_revision=sum(r["revised"]["selected"] == child for r in local),
            identical_first_requests=sum(
                r["previous"]["first_solver_request"] == r["revised"]["first_solver_request"] for r in paired
            ),
        )
    return results


def submitted(rows):
    inventory = {}
    for r in rows:
        if not r["patch"]:
            continue
        tid = r["intent"]["task"]
        item = dict(
            task=tid,
            snapshot=r["intent"]["snapshot"],
            patch=r["patch"],
            passed=r["passed"],
            probes=r["probes"],
        )
        key = (tid, r["patch"])
        require(inventory.setdefault(key, item) == item, "one patch has incompatible grades")
    return [inventory[k] for k in sorted(inventory)]


def replay_manifest(rows):
    items = submitted(rows)
    return dict(unique=len(items), named=sum(x["probes"] is not None for x in items), inventory=digest(items))


def replay_submissions(s, rows):
    checks = {(r["intent"]["task"], r["patch"]): r["public_feedback"] for r in rows if r["public_feedback"]}

    def one(item):
        task = s.task(item["task"])
        patch = s.store.get_artifact(item["patch"])
        require(
            set(patch) == set(task.editable) and digest(patch) == item["patch"], "changed submitted files"
        )
        if item["probes"] is not None:
            checks_now = probe_patch(task, patch, s.cfg)
            require(
                checks_now == item["probes"] and all(checks_now.values()) == item["passed"],
                "independent probe mismatch",
            )
        else:
            public = verify_public(task, patch, s.cfg, "replay-" + item["patch"][:12])
            graded = grade(task, patch, s.cfg, "replay-" + item["patch"][:12])
            require(
                not public["timeout"]
                and not graded["timeout"]
                and public["image_id"] == graded["image_id"] == s.p["image"]
                and (public["exit_code"] == 0 and graded["passed"]) == item["passed"],
                "independent grader mismatch",
            )
        check = checks.get((task.id, item["patch"]))
        if check:
            require(
                public_check(task, patch, s.new[task.id][2], s.cfg) == check,
                "learning feedback replay differs",
            )

    bounded_map(one, submitted(rows), WORKERS)
    return replay_manifest(rows)


def example_runs(learn, choice, index):
    # Small learn-only illustration. These excerpts have no role in the gate or retention decision.
    keys = []
    flag = next(
        (key for key in learn["flag_misroutes"] if index[key]["passed"] is False and index[key]["patch"]),
        None,
    )
    if flag:
        keys.append(flag)
    keys.extend(learn["wake_failures"][:1])
    if choice and choice.get("proposal") and choice["proposal"]["child"]:
        alternate = choice["proposal"]["child"]
        seen = set()
        for key in choice["matched"]:
            r = index[key]
            t = r["intent"]["task"]
            if (
                alternate in r["intent"]["pool"]
                and t in WAKES
                and t not in seen
                and r["patch"]
                and r["passed"] is False
            ):
                keys.append(key)
                seen.add(t)
    return keys


def paid_stages(data, p):
    groups = {}
    for n, learn in enumerate(data["learning"], 1):
        groups[f"learning_{n}"] = learn["runs"]
        groups[f"proposal_{n}"] = learn["proposal"]["attempts"]
    groups["parent_screen"] = [rid for family in data["screen"]["families"] for rid in family["runs"]]
    choice = data.get("selection")
    if choice:
        groups["scope_audit"] = choice["audit"]
        if choice.get("proposal"):
            groups["role_aware_proposal"] = choice["proposal"]["attempts"]
        groups["matched_learning"] = choice["matched"]
    for n in range(7):
        groups[f"panel_round_{n + 1}"] = [
            r["run"]
            for r in data["rows"]
            if r["intent"]["phase"] in ("gate", "protection") and r["intent"]["repeat"] == n
        ]
    for fam, ids in p["holdout"].items():
        groups["holdout_" + fam] = [
            r["run"] for r in data["rows"] if r["intent"]["phase"] == "holdout" and r["intent"]["task"] in ids
        ]
    return {k: v for k, v in groups.items() if v}


def check_call_order(data, p):
    groups = paid_stages(data, p)
    by_run = {r["run"]: r["cost"]["calls"] for r in data["rows"]} | {
        r: c["calls"] for r, c in data["proposal_billing"].items()
    }
    expected = []
    last = 0
    assigned = []
    for stage, ids in groups.items():
        assigned.extend(ids)
        count = sum(by_run[k] for k in ids)
        expected.append(dict(stage=stage, first=last + 1, last=last + count, calls=count))
        last += count
    require(
        len(assigned) == len(set(assigned))
        and set(assigned) == set(by_run)
        and data["call_order"] == expected,
        "call order diverges from registered stages or contains reused runs",
    )


def check_examples(data, execute=False):
    rows = {r["run"]: r for r in data["rows"]}
    learning = data["learning"][-1]
    expected = example_runs(learning, data.get("selection"), rows)
    programs = cases()
    require(list(data["learning_patches"]) == expected, "different illustrative learning examples")
    for key, patch in data["learning_patches"].items():
        r = rows[key]
        tid = r["intent"]["task"]
        t, _, program = programs[tid]
        require(
            r["intent"]["phase"] == "learn" and digest(patch) == r["patch"] and set(patch) == set(t.editable),
            "example does not match the submitted learning patch",
        )
        if execute:
            settings = Settings(image=read(PUBLIC)["image"])
            require(
                probe_patch(t, patch, settings) == r["probes"]
                and public_check(t, patch, program, settings) == r["public_feedback"],
                "example's executable results differ",
            )
    return len(expected)


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
    require(
        len(index) == len(rows) and data["patch_replay"] == replay_manifest(rows),
        "reused run or mismatched patch inventory",
    )
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
    require(
        data["revision_comparison"] == revision_comparison(panels, h["child"]),
        "changed direct old/new comparison",
    )
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
    check_call_order(data, p)
    summary = {}
    for phase in ("learn", "screen", "protection", "gate", "holdout"):
        counts = Counter(r["passed"] for r in rows if r["intent"]["phase"] == phase)
        if counts:
            summary[phase] = dict(
                solves=sum(counts.values()), passed=counts[True], failed=counts[False], ungraded=counts[None]
            )
    return dict(
        phases=summary,
        mode=h["mode"],
        published=allow,
        exposures=exposures,
        billing=data["billing"],
        replayable_learning_examples=check_examples(data),
    )


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
        patch_replay=replay_submissions(s, rows),
        revision_comparison=revision_comparison(panel_rows(rows, h), h["child"]),
        revision_receipts=receipts,
        gate=gate,
        protection=direct,
        activation=activation,
        holdout=read(ROOT / "holdout.json") if (ROOT / "holdout.json").exists() else None,
        holdout_exposures=len(exposures),
        proposal_billing={rid: s.store.costs([rid]) for rid in proposals},
        billing=s.store.costs(sorted(ids)),
    )
    index = {r["run"]: r for r in rows}
    d["learning_patches"] = {
        k: s.store.get_artifact(index[k]["patch"]) for k in example_runs(stages[-1], choice, index)
    }
    membership = {rid: stage for stage, items in paid_stages(d, s.p).items() for rid in items}
    chronology = []
    own = [r for r in recent if r["run_id"] in ids]
    for seq, r in enumerate(own, 1):
        label = membership[r["run_id"]]
        if not chronology or chronology[-1]["stage"] != label:
            chronology.append(dict(stage=label, first=seq, last=seq, calls=0))
        chronology[-1]["last"] = seq
        chronology[-1]["calls"] += 1
    d["call_order"] = chronology
    d["summary"] = review(d, s.p, h)
    dump(RESULT, d)
    return d["summary"]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["export", "verify", "replay-examples"])
    args = parser.parse_args()
    if args.command == "export":
        with session() as study:
            print(export(study))
    else:
        data = read(RESULT)
        actual = review(data, read(PUBLIC), read(FREEZE))
        require(actual == data["summary"], "different summary")
        if args.command == "replay-examples":
            print("Docker-verified learning patches", check_examples(data, execute=True))
        print(actual)
