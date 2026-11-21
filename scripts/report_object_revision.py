"""Verify the object-revision observations offline, or bind and reproduce them from the private archive."""

import argparse
from collections import Counter
from decimal import Decimal
from pathlib import Path

from evocoder.comparison import dump
from evocoder.contracts import Settings, Skill, digest
from evocoder.dataset import load_catalog
from evocoder.evolution import Evolution
from evocoder.gate import gate_summary, skill_behavior
from evocoder.grounding import build_grounding, validate_summary
from evocoder.jobs import bounded_map
from falsy_transfer_protocol import FAMILIES, LEARNED, REFERENCE, probe_patch
from object_revision_cases import cases
from object_witness import matched_learning, validate_program, verdict
from report_click_procedure import Archive, read
from report_falsy_transfer import GATE_FIELDS, arm_summary, recalibrate
from run_falsy_transfer import require
from run_object_revision import (
    CONFIG_ARMS,
    OBJECT_ARMS,
    OLD,
    POLICY,
    PUBLIC,
    order,
    release_decision,
    select_evidence,
)
from run_optional_control import curve_point

RESULT = Path("docs/results/object-revision.json")
FIELDS = ("steps", "applicability", "verification", "exclusions", "counterexamples")


def aggregate(study, protocol):
    learning = {}
    for arm in ("parent", "fixed_parent"):
        selected = [r[arm] for r in study.get("learn", []) if arm in r["order"]]
        learning[arm] = {
            "runs": len(selected),
            "passed": sum(r["passed"] for r in selected),
            "applied": sum(r["applied"] is not None for r in selected),
            "eligible": sum(r["eligible"] for r in selected),
        }
    out = {"learning": learning} if "learn" in study else {}
    if study.get("routing_trials"):
        out["learning_scope_check"] = arm_summary(study["routing_trials"], "child")
    for group, arms in (("objects", OBJECT_ARMS), ("config", CONFIG_ARMS)):
        rows = [r for r in study["dev"] if r["task"] in protocol[group]]
        if not rows:
            continue
        out[group] = {
            a: arm_summary(rows, a)
            | {"by_repeat": [sum(r[a]["passed"] for r in rows if r["repeat"] == n) for n in range(2)]}
            for a in arms
        }
        out[group + "_pairs"] = {}
        for base, candidate in [
            ("bare", "child"),
            ("parent", "child"),
            *([("fixed_parent", "fixed_child"), ("manual", "child")] if group == "objects" else []),
        ]:
            wins = [r for r in rows if not r[base]["passed"] and r[candidate]["passed"]]
            out[group + "_pairs"][base + "_to_" + candidate] = {
                "wins": len(wins),
                "losses": sum(r[base]["passed"] and not r[candidate]["passed"] for r in rows),
                "same_first_request": sum(
                    r[base]["first_solver_request"] == r[candidate]["first_solver_request"] for r in rows
                ),
                "repeated_wins": sorted(t for t, n in Counter(r["task"] for r in wins).items() if n == 2),
            }
    if "objects" in out:
        normal, manual = out["objects"]["child"]["passed"], out["objects"]["manual"]["passed"]
        out["normal_near_manual"] = normal >= 5 and normal >= manual - 1
    return out


def compact_gate(g):
    return {
        "receipt": g["receipt"],
        "library_id": g["library_id"],
        "baseline": g["baseline_members"],
        "candidate": g["candidate"],
        "rows": [{k: r[k] for k in GATE_FIELDS} for r in g["rows"]],
        "decision": gate_summary(g["rows"], 2),
    }


def check_witness(d, p, first, seen):
    require(
        first["phase"] == "gate_rejected"
        and not first["published"]
        and not first["holdout_exposures"]
        and first["protocol"] == p["previous"]
        and first["proposal"]["child"] == p["previous_child"]
        and digest(first["learn"]) == p["learning_receipts"]
        and matched_learning(first["learn"]) == p["matched"],
        "incorrect prior experiment",
    )
    matched = p["matched"]
    task, ref = next((t, f) for t, f in cases() if t.id == matched["task"])
    attempts = d["witness_attempts"]
    require(1 <= len(attempts) <= 3, "missing check drafts")
    require(
        set(d["witness_requests"]) == {a["run"] for a in attempts}
        and len(set(d["witness_requests"].values())) == 1
        and all(len(h) == 64 for h in d["witness_requests"].values()),
        "subsequent checks received different inputs",
    )
    previous = 0
    accepted = []
    for i, a in enumerate(attempts):
        rid = "check-" + digest({"protocol": digest(p), "attempt": i})[:32]
        require(
            a["run"] == rid and rid not in seen and d["billing"][rid]["first"] > previous,
            "wrong check attempt",
        )
        seen.add(rid)
        previous = d["billing"][rid]["last"]
        if "program" in a:
            validate_program(a["program"], task)
            good = a["statuses"] == {
                "failure": "assertion_failed",
                "success": "passed",
                "reference": "passed",
            }
            require(a["hash"] == digest(a["program"]) and good == a["accepted"], "wrong witness verdict")
        else:
            require(not a["accepted"] and a.get("invalid"), "missing authored test")
        if a["accepted"]:
            accepted.append(i)
    observe = d["witness"]
    require(
        (accepted == [len(attempts) - 1] if observe else len(attempts) == 3 and not accepted),
        "chose a later discriminating test",
    )
    if any("program" in a for a in attempts):
        originals = {r[a]["run"]: r[a]["patch"] for r in first["learn"] for a in r["order"]}
        patches = d["witness_patches"]
        require(
            all(
                set(patches[a]) == set(task.editable) and digest(patches[a]) == originals[matched[a]]
                for a in ("failure", "success")
            )
            and digest(ref) == next(r["reference"] for r in p["calibration"] if r["id"] == task.id),
            "incorrect public replay snapshot",
        )
    if observe:
        a = attempts[-1]
        expected = {
            "task": task.id,
            "task_hash": digest(task.model_dump()),
            "program": a["program"],
            "program_hash": a["hash"],
            "on_failure": "assertion_failed",
            "on_success": "passed",
        }
        for k in ("failure", "success"):
            expected[k + "_run"] = matched[k]
            expected[k + "_patch"] = digest(d["witness_patches"][k])
        require(observe == expected, "changed evidence provided to proposer")
    return previous


def replay_witness(d, p, image):
    task, reference = next((t, f) for t, f in cases() if t.id == p["matched"]["task"])
    import subprocess

    image_id = subprocess.check_output(
        ["docker", "image", "inspect", "--format", "{{.Id}}", image], text=True
    ).strip()
    cfg = Settings(_env_file=None, mode="mock", image=image_id)
    programs = [a for a in d["witness_attempts"] if "program" in a]
    for a in programs:
        states = {
            k: verdict(task, f, a["program"], cfg)
            for k, f in (d["witness_patches"] | {"reference": reference}).items()
        }
        require(states == a["statuses"], "public differential test does not reproduce")
    return len(programs)


def verify(d, p):
    require(
        d["protocol"] == digest(p)
        and p["parent"] == LEARNED
        and p["manual"] == REFERENCE.hash
        and all(digest(read(OLD / (k + ".json"))) == v for k, v in p["published_observations"].items()),
        "incorrect registered study or historical baseline",
    )
    tasks = {t["id"]: t for t in p["tasks"]}
    require(
        len(tasks) == len(p["tasks"])
        and not {tasks[t]["source_group"] for t in p["learn"]}
        & {v["source_group"] for k, v in tasks.items() if k not in p["learn"]},
        "leaked learn source",
    )
    history = read(OLD / "optional-control.json")
    (root,) = [s["content"] for s in history["skills"] if s["id"] == LEARNED]
    require(digest(root) == LEARNED, "unbound original skill")
    bills, seen = d["billing"], set()
    normal_routes = set()
    proposal = d.get("proposal")
    child = proposal and proposal["child"]
    cal = {r["id"]: set(r["fixed"]) for r in p["calibration"]}
    for name in ("object-panel", "protocol"):
        for row in read(Path("benchmarks/falsy_transfer") / (name + ".json"))["calibration"]:
            cal[row["task"]] = set(row["fixed"])

    def observe(arm, a, task, learn):
        key = {
            "bare": None,
            "parent": LEARNED,
            "fixed_parent": LEARNED,
            "child": child,
            "fixed_child": child,
            "manual": REFERENCE.hash,
        }[arm]
        rid = a["run"]
        require(
            rid not in seen
            and a["state"] in {"COMPLETE", "FAILED", "EXHAUSTED"}
            and a["passed"] == (a["state"] == "COMPLETE")
            and a["calls"] == bills[rid]["calls"]
            and 0 <= a["steps"] <= p["solver_slots"]
            and a["steps"] <= a["calls"]
            and len(a["first_solver_request"]) == 64,
            "invalid trial receipt",
        )
        seen.add(rid)
        if arm.startswith("fixed_") or arm == "manual":
            require(a["route"] == "forced" and a["applied"] == key, "invalid controlled intervention")
        elif arm == "bare":
            require(a["route"] == "none" and a["applied"] is None, "invalid unassisted control")
        else:
            normal_routes.add(rid)
            require(
                a["applied"] in {None, key} and (a["route"] == "apply") == (a["applied"] == key),
                "normal routing mismatch",
            )
            note = d["route_notes"][rid]
            require(
                note["decision"] == a["route"]
                and note["requested"] == [key]
                and isinstance(note["reason"], str)
                and len(note["reason"]) <= 2000,
                "unbound router explanation",
            )
        if a["patch"]:
            require(
                set(a["probes"]) == cal[task]
                and all(type(x) is bool for x in a["probes"].values())
                and all(a["probes"].values()) == a["passed"],
                "patch probes disagree with grade",
            )
        else:
            require(a["state"] == "EXHAUSTED" and not a.get("probes"), "unsupported submitted patch")
        if learn:
            fb = a["feedback"]
            qualifies = bool(
                fb["post_edit_public_failure_sequences"]
                or fb["step_budget_exhausted"]
                or fb["command_budget_exhausted"]
                or fb["learn_submission_failed_independent_check"]
            )
            require(
                a["eligible"] == (key == a["applied"] == LEARNED and not a["passed"] and qualifies),
                "ineligible parent exposure",
            )

    witness = "matched" in p
    phases = [] if witness else [("learn", p["learn"])]
    phases.append(("dev", p["objects"] + p["config"] if child else []))
    for phase, ids in phases:
        expected = {(k, n) for n in range(2) for k in ids}
        rows = d[phase]
        require(
            len(rows) == len(expected) and {(r["task"], r["repeat"]) for r in rows} == expected,
            "missing or duplicated comparison",
        )
        for r in rows:
            tid, n = r["task"], r["repeat"]
            if phase == "learn":
                seq = order(("parent", "fixed_parent"), ids.index(tid), 0) if n == 0 else ["fixed_parent"]
            else:
                objects = tid in p["objects"]
                seq = order(
                    OBJECT_ARMS if objects else CONFIG_ARMS,
                    p["objects" if objects else "config"].index(tid),
                    n,
                )
            require(r["order"] == seq, "incorrect arm order")
            last = 0
            for a in seq:
                v = r[a]
                observe(a, v, tid, phase == "learn")
                b = bills[v["run"]]
                require(b["first"] > last, "actual arm timing differs")
                last = b["last"]
    if witness:
        first = read(RESULT)
        verify(first, read(PUBLIC))
        last_before_proposal = check_witness(d, p, first, seen)
        rows, refs = first["learn"], p["matched"]["refs"]
        require(bool(d["witness"]) == bool(proposal), "proposal without a verified public counterexample")
        require(
            [r["task"] for r in d["routing_trials"]] == (p["learn"] if child else []),
            "incomplete scope diagnostic",
        )
        for r in d["routing_trials"]:
            observe("child", r["child"], r["task"], False)
    else:
        rows = d["learn"]
        refs = select_evidence(rows)
        last_before_proposal = max(bills[r[a]["run"]]["last"] for r in rows for a in r["order"])
        require(bool(refs) == bool(proposal), "changed revision eligibility")
    require(refs == d["evidence_runs"], "changed revision fuel")
    learned = {r[a]["run"]: (r["task"], r[a]) for r in rows for a in r["order"]}
    if proposal:
        ids = proposal["attempts"]
        audits = d["proposal_audit"]
        require(
            [a["run"] for a in audits] == ids
            and len({a["request"] for a in audits}) == 1
            and all(len(a["request"]) == 64 for a in audits)
            and [i for i, a in enumerate(audits) if a["accepted"]] == ([len(ids) - 1] if child else []),
            "proposal changed input or bypassed rejection",
        )
        require(
            1 <= len(set(ids)) == len(ids) <= 2
            and seen.isdisjoint(ids)
            and last_before_proposal < min(bills[i]["first"] for i in ids),
            "learning evidence or proposal timing invalid",
        )
        seen.update(ids)
    if child:
        lineage = d["revision"]
        content = Skill.model_validate(lineage["content"])
        require(
            lineage["proposal_request"] == d["proposal_audit"][-1]["request"]
            and not d["proposal_audit"][-1]["outside_evidence_pool"],
            "unbound admitted proposal",
        )
        require(
            content.hash == child
            and content.parent == LEARNED
            and set(content.evidence_refs) <= set(refs)
            and any(learned[r][1]["eligible"] for r in content.evidence_refs),
            "child/evidence binding invalid",
        )
        changed = {
            f: {"before": digest(root[f]), "after": digest(getattr(content, f))}
            for f in FIELDS
            if root[f] != getattr(content, f)
        }
        att = lineage["receipt"]
        require(
            changed
            and att["changed_fields"] == changed
            and digest(att) == proposal["receipt"]
            and att["frozen_evidence"] == proposal["evidence"]
            and att["candidate"] == child
            and att["parent"] == LEARNED
            and att["evidence_refs"] == content.evidence_refs
            and att["summary_hash"] == digest(content.change_summary)
            and digest(lineage["catalog"]) == att["catalog"],
            "changed historical claims",
        )
        validate_summary(content.change_summary, content.evidence_refs, lineage["catalog"])
        if witness:
            require(
                lineage["witness_hash"] == digest(d["witness"]) and child != p["previous_child"],
                "new child lacks the witnessed learn input",
            )
            ordered = [bills[r["child"]["run"]] for r in d["routing_trials"]]
            require(
                max(bills[i]["last"] for i in proposal["attempts"]) < min(x["first"] for x in ordered)
                and max(x["last"] for x in ordered)
                < min(bills[r[a]["run"]]["first"] for r in d["dev"] for a in r["order"]),
                "scope diagnostic order changed",
            )
        support = set(p["root_support"]) | {learned[k][0] for k in refs}
        expected = {(t, n) for t in p["config"] + p["objects"] for n in range(2)} | {(t, 0) for t in support}
        dev = {(r["task"], r["repeat"]): r for r in d["dev"]}
        shared_child = {}
        for name, base in (("parent_gate", "parent"), ("library_gate", "bare")):
            g = d[name]
            require(
                g["candidate"] == child
                and g["baseline"] == ([LEARNED] if base == "parent" else [])
                and len(expected) == len(g["rows"])
                and {(r["task_id"], r["repeat"]) for r in g["rows"]} == expected
                and g["decision"] == gate_summary(g["rows"], 2),
                "wrong gate population or decision",
            )
            for r in g["rows"]:
                t, n = k = r["task_id"], r["repeat"]
                require(
                    (r["task_hash"], r["source_group"], r["split"])
                    == (tasks[t]["hash"], tasks[t]["source_group"], tasks[t]["split"])
                    and r["candidate_applied_skill"] in {None, child}
                    and r["baseline_applied_skill"] in ({None, LEARNED} if base == "parent" else {None})
                    and r["skill_behavior"] == skill_behavior(r, child, set(g["baseline"])),
                    "wrong gate row",
                )
                if k in dev:
                    for label, arm in (("baseline", base), ("candidate", "child")):
                        v = dev[k][arm]
                        require(
                            (r[label + "_run"], r[label + "_pass"], r[label + "_applied_skill"])
                            == (v["run"], v["passed"], v["applied"]),
                            "forced or historical arm entered gate",
                        )
                else:
                    rid, cid = r["baseline_run"], r["candidate_run"]
                    require(
                        rid not in seen
                        and (k in shared_child or cid not in seen)
                        and shared_child.setdefault(
                            k, (cid, r["candidate_pass"], r["candidate_applied_skill"])
                        )
                        == (cid, r["candidate_pass"], r["candidate_applied_skill"]),
                        "inconsistent support replay",
                    )
                    seen.update((rid, cid))
        realized = release_decision(
            [r for r in d["parent_gate"]["rows"] if r["task_id"] in p["objects"]],
            d["parent_gate"]["decision"],
            d["library_gate"]["decision"],
        )
        require(
            realized == d["decision"]
            and d["published"] == realized["passed"]
            and max(bills[i]["last"] for i in proposal["attempts"])
            < min(bills[r[a]["run"]]["first"] for r in d["dev"] for a in r["order"]),
            "invalid release or revised after grading",
        )
    else:
        require(
            not d["published"]
            and not any(k in d for k in ("revision", "decision", "parent_gate", "library_gate")),
            "tested an absent child",
        )
    expected_phase = (
        "complete"
        if d["published"]
        else "gate_rejected"
        if child
        else "proposal_rejected"
        if proposal
        else "no_distinguishing_public_check"
        if witness
        else "no_qualifying_application"
    )
    require(d["phase"] == expected_phase, "wrong terminal phase")
    ids = [t for f in FAMILIES for t in p["holdout"][f]] if d["published"] else []
    require(
        [r["task_id"] for r in d["holdout"]] == ids and d["holdout_exposures"] == len(ids), "premature test"
    )
    points, accumulated = [], []
    if ids:
        final_dev = max(bills[r]["last"] for r in seen)
        for family in FAMILIES:
            rows = [r for r in d["holdout"] if r["family"] == family]
            require(len(rows) == 10 and [r["task_id"] for r in rows] == p["holdout"][family], "incorrect mix")
            for r in rows:
                require(
                    r["split"] == "test"
                    and r["repeat"] == 0
                    and not r["infrastructure_error"]
                    and r["task_hash"] == tasks[r["task_id"]]["hash"]
                    and r["baseline_applied_skill"] is None
                    and r["candidate_applied_skill"] in {None, child}
                    and r["skill_behavior"] == skill_behavior(r, child, set()),
                    "invalid held-out result",
                )
                for k in ("baseline_run", "candidate_run"):
                    require(
                        r[k] not in seen and bills[r[k]]["first"] > final_dev, "test ran before final gate"
                    )
                    seen.add(r[k])
            accumulated.extend(rows)
            points.append(curve_point(accumulated, family, rows))
    require(d["curve"] == points, "unobserved curve")
    require(set(d["route_notes"]) == normal_routes, "missing or unexpected normal routing explanation")
    seen.update(r["failed_run"] for r in d["transport"])
    require(
        set(bills) == seen
        and all(
            0 < b["calls"] <= b["last"] - b["first"] + 1
            and b["first"] >= 1
            and b["charged_nanos"] >= 0
            and b["reserved_nanos"] >= 0
            for b in bills.values()
        ),
        "incomplete accounting",
    )
    for name, key, factor in (
        ("calls", "calls", 1),
        ("charged_nanos", "cost_credits", 10**9),
        ("reserved_nanos", "reserved_credits", 10**9),
    ):
        require(
            sum(b[name] for b in bills.values()) == Decimal(str(d["costs"][key])) * factor, "wrong model cost"
        )
    require(d["summary"] == aggregate(d, p), "incorrect repair summary")
    return {"phase": d["phase"], "summary": d["summary"], "holdout": len(ids), "costs": d["costs"]}


def export(root, p):
    raw = read(root / "result.json")
    require(
        raw["completed"] and raw["unassigned"] == 0 and read(root / "protocol.json") == p,
        "incomplete archive",
    )
    s, m = [Archive(root / n / "evocoder.sqlite3") for n in ("state", "manual-state")]
    tasks = {t.id: t for t in load_catalog(root / "dataset/manifest.json")}
    require(
        [
            {
                "id": t.id,
                "path": t.id + ".json",
                "hash": digest(t.model_dump()),
                "source_group": t.source_group,
                "split": t.split,
            }
            for t in tasks.values()
        ]
        == p["tasks"],
        "changed archived snapshot",
    )
    start = read(root / "start.json")["cursor"]
    with s.connect() as db:
        binding = db.execute("SELECT root FROM ledger_binding").fetchone()[0]
        actual_tests = {r[0] for r in db.execute("SELECT id FROM runs WHERE split='test'")}
        releases = list(db.execute("SELECT after_id,created FROM library_releases ORDER BY seq"))
    with m.connect() as db:
        manual_ids = {r[0] for r in db.execute("SELECT id FROM runs")}
        require(not db.execute("SELECT 1 FROM runs WHERE split='test'").fetchone(), "manual holdout exposure")
    ledger = Archive(Path(binding) / "evocoder.sqlite3")
    proposal = raw.get("proposal")
    attempts = set(proposal["attempts"] if proposal else [])
    slots = {}
    for f in (root / "slots").glob("*.json"):
        intent = read(f)
        require(
            digest(intent)[:32] == f.stem
            and intent["protocol"] == digest(p)
            and intent["task_hash"] == digest(tasks[intent["task"]].model_dump()),
            "incorrect slot",
        )
        slots[f.stem] = intent
    keys = slots.keys() | attempts | {a["run"] for a in raw.get("witness_attempts", [])}
    with ledger.connect() as db:
        incoming = list(
            db.execute(
                "SELECT rowid,run_id,actual,reserved,state FROM calls WHERE rowid>? ORDER BY rowid", (start,)
            )
        )
        exposed = {
            r[0] for r in db.execute("SELECT task_id FROM test_exposures WHERE experiment=?", (digest(p),))
        }
    bills = {}
    for i, rid, amount, reserved, state in incoming:
        if rid not in keys:
            continue
        b = bills.setdefault(
            rid, {"first": i - start, "last": i - start, "calls": 0, "charged_nanos": 0, "reserved_nanos": 0}
        )
        b["last"] = i - start
        b["calls"] += 1
        b["charged_nanos"] += amount or 0
        b["reserved_nanos"] += reserved if state == "reserved" else 0
    require(
        keys == bills.keys()
        and sum(b["calls"] for b in bills.values()) == max(b["last"] for b in bills.values()),
        "unreported or interleaved charges",
    )
    out = {
        k: raw[k]
        for k in (
            "protocol",
            "phase",
            "published",
            "evidence_runs",
            "dev",
            "holdout_exposures",
            "curve",
            "costs",
            "transport",
        )
    }
    if "matched" in p:
        for k in ("witness", "witness_attempts", "routing_trials"):
            out[k] = raw[k]
        out["witness_requests"] = {}
        for a in raw["witness_attempts"]:
            events = s.events(a["run"])
            calls = [e["payload"] for e in events if e["kind"] == "MODEL_RESPONSE"]
            require(
                calls
                and all(
                    c["provider_model"].lower() == p["model"].lower()
                    and c["expected_function"] == "submit_action"
                    for c in calls
                ),
                "witness not authored by live model",
            )
            require(len(calls) == 1, "unexpected extra call while drafting one check")
            out["witness_requests"][a["run"]] = calls[0]["request_body_hash"]
            if "program" in a:
                (draft,) = [e["payload"] for e in events if e["kind"] == "CHECK_DRAFT"]
                require(
                    s.get_artifact(draft["artifact"]) == {"program": a["program"]}, "changed authored test"
                )
        if any("program" in a for a in raw["witness_attempts"]):
            out["witness_patches"] = {
                k: s.get_artifact(s.run(p["matched"][k])["result"]["patch"]) for k in ("failure", "success")
            }
            replay_witness(out, p, p["image"])
    else:
        out["learn"] = raw["learn"]
    out["holdout"] = [{k: r[k] for k in (*GATE_FIELDS, "family")} for r in raw["holdout"]]
    out["billing"] = bills
    if proposal:
        out["proposal"] = proposal
        ev = s.get_artifact(proposal["evidence"])
        out["proposal_audit"] = []
        for rid in proposal["attempts"]:
            events = s.events(rid)
            model_replies = [e["payload"] for e in events if e["kind"] == "MODEL_RESPONSE"]
            require(
                events[0]["kind"] == "PROPOSAL_START"
                and events[0]["payload"]["evidence_hash"] == proposal["evidence"]
                and events[0]["payload"]["projection_hash"] == digest(Evolution.project(ev))
                and events[-1]["kind"] in {"PROPOSED", "PROPOSAL_ERROR"}
                and len(model_replies) == 1
                and model_replies[0]["provider_model"].lower() == p["model"].lower(),
                "incorrect proposal inputs, model or terminal state",
            )
            output = [e["payload"] for e in events if e["kind"] == "PROPOSAL_OUTPUT"]
            cited = s.get_artifact(output[0]["artifact"])["evidence_refs"] if output else []
            out["proposal_audit"].append(
                {
                    "run": rid,
                    "request": model_replies[0]["request_body_hash"],
                    "accepted": events[-1]["kind"] == "PROPOSED",
                    "outside_evidence_pool": bool(set(cited) - set(raw["evidence_runs"])),
                }
            )
        if proposal["child"]:
            child = proposal["child"]
            att = s.get_artifact(proposal["receipt"])
            ev = s.get_artifact(proposal["evidence"])
            catalog = build_grounding(s, ev, tasks)
            require(
                ev["parent_id"] == LEARNED
                and [e["run_id"] for e in ev["episodes"]] == raw["evidence_runs"]
                and all(
                    s.run(e["run_id"])["split"] == "learn" and s.run(e["run_id"])["purpose"] == "solve"
                    for e in ev["episodes"]
                )
                and catalog == s.get_artifact(att["catalog"]),
                "proposal evidence or summary not grounded",
            )
            proposal_events = s.events(proposal["attempts"][-1])
            calls = [e["payload"] for e in proposal_events if e["kind"] == "MODEL_RESPONSE"]
            require(
                proposal_events[0]["kind"] == "PROPOSAL_START"
                and proposal_events[0]["payload"]["proposal_policy"] == POLICY
                and proposal_events[-1]["kind"] == "PROPOSED"
                and proposal_events[-1]["payload"]["skill_id"] == child
                and calls
                and all(c["provider_model"].lower() == p["model"].lower() for c in calls)
                and any(
                    e["kind"] == "SUMMARY_VERIFIED" and e["payload"]["artifact"] == proposal["receipt"]
                    for e in proposal_events
                ),
                "missing live model/summary receipt",
            )
            out["revision"] = {
                "content": s.get_artifact(child),
                "receipt": att,
                "catalog": catalog,
                "proposal_request": calls[-1]["request_body_hash"],
            }
            if "matched" in p:
                (bound,) = [
                    e["payload"]["receipt"] for e in proposal_events if e["kind"] == "PUBLIC_LEARN_CONTRAST"
                ]
                require(s.get_artifact(bound) == out["witness"], "wrong model check in proposal")
                out["revision"]["witness_hash"] = bound
            for name in ("parent_gate", "library_gate"):
                g = raw[name]
                if name == "library_gate":
                    require(
                        s.get_artifact(g["receipt"]) == {k: v for k, v in g.items() if k != "receipt"},
                        "unbound library decision",
                    )
                out[name] = compact_gate(g)
            out["decision"] = raw["decision"]
    seen_tests = {r[k] for r in out["holdout"] for k in ("baseline_run", "candidate_run")}
    require(
        seen_tests | {t["failed_run"] for t in raw["transport"] if t["phase"] == "holdout"} == actual_tests
        and exposed == {r["task_id"] for r in out["holdout"]}
        and bool(releases) == out["published"]
        and (
            not releases
            or (
                len(releases) == 1
                and releases[0][0] == raw["library_gate"]["library_id"]
                and s.active_library() == releases[0][0]
                and all(releases[0][1] <= s.run(k)["created"] for k in seen_tests)
            )
        ),
        "incorrect holdout or release history",
    )
    cfg = Settings(_env_file=None, mode="mock", image=p["image"])
    to_probe = {}
    out["route_notes"] = {}
    for phase in ("learn", "dev", "routing_trials"):
        for row in raw.get(phase, []):
            for arm in ["child"] if phase == "routing_trials" else row["order"]:
                a, t = row[arm], tasks[row["task"]]
                st = m if arm == "manual" else s
                rid = a["run"]
                require((rid in manual_ids) == (arm == "manual"), "wrong isolated history")
                run, events = st.run(rid), st.events(rid)
                f = run["result"]
                (route,) = [e["payload"] for e in events if e["kind"] == "SKILL_ROUTE"]
                if arm in ("parent", "child"):
                    out["route_notes"][rid] = {
                        "decision": route["decision"],
                        "requested": route["requested_skill_ids"],
                        "condition_indices": route.get("condition_indices", []),
                        "exclusion_indices": route.get("exclusion_indices", []),
                        "reason": route.get("reason", ""),
                    }
                replies = [e["payload"] for e in events if e["kind"] == "MODEL_RESPONSE"]
                first = next(c for c in replies if c["expected_function"] == "submit_action")
                require(
                    run["task_hash"] == slots[rid]["task_hash"] == digest(t.model_dump())
                    and run["purpose"] == ("solve" if phase == "learn" else "evaluate")
                    and run["mode"] == "live"
                    and f["image_id"] == p["image"]
                    and all(c["provider_model"].lower() == p["model"].lower() for c in replies)
                    and (
                        a["state"],
                        a["passed"],
                        a["patch"],
                        a["applied"],
                        a["route"],
                        a["first_solver_request"],
                    )
                    == (
                        run["state"],
                        f["passed"],
                        f["patch"],
                        run["skill_id"],
                        route["decision"],
                        first["request_body_hash"],
                    )
                    and a["steps"] == sum(e["kind"] == "ACTION" for e in events)
                    and a["calls"] == f["cost"]["calls"],
                    "trial differs from archived execution",
                )
                if phase == "learn":
                    # Replaying feedback only reads events; the archive has no live model or billing session.
                    feedback = Evolution(
                        cfg, st, model=object(), feedback_policy="learn_outcome"
                    ).revision_feedback(run, events)
                    require(feedback == a["feedback"], "learning feedback disagrees with executed actions")
                if a["patch"]:
                    token = (t.id, a["patch"])
                    patch = st.get_artifact(a["patch"])
                    require(
                        token not in to_probe or to_probe[token][2] == a["probes"], "conflicting patch result"
                    )
                    to_probe[token] = (t, patch, a["probes"])

    def run_probes(item):
        t, patch, claimed = item
        require(probe_patch(t, patch, cfg) == claimed, "unreproducible submitted patch on " + t.id)

    bounded_map(run_probes, list(to_probe.values()), 4)
    for r in [
        *out.get("parent_gate", {}).get("rows", []),
        *out.get("library_gate", {}).get("rows", []),
        *out["holdout"],
    ]:
        for a in ("baseline", "candidate"):
            run = s.run(r[a + "_run"])
            require(
                run["task_hash"] == r["task_hash"]
                and run["purpose"] == "evaluate"
                and run["mode"] == "live"
                and run["result"]["image_id"] == p["image"]
                and run["result"]["passed"] == r[a + "_pass"]
                and run["skill_id"] == r[a + "_applied_skill"],
                "gate differs from archived executions",
            )
    out["summary"] = aggregate(out, p)
    verify(out, p)
    return out


if __name__ == "__main__":
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("command", choices=["verify", "export", "calibrate", "replay"])
    cli.add_argument("--study", choices=["revision", "witness"], default="revision")
    cli.add_argument("--root", type=Path)
    cli.add_argument("--registration", type=Path)
    cli.add_argument("--result", type=Path)
    cli.add_argument("--image", default="python:3.12-slim")
    args = cli.parse_args()
    base = "object-" + args.study
    p = read(args.registration or PUBLIC.with_name(base + ".json"))
    path = args.result or RESULT.with_name(base + ".json")
    if args.command == "calibrate":
        reg = {
            "calibration": [dict(r, task=r["id"], reference_patch=r["reference"]) for r in p["calibration"]]
        }
        recalibrate(reg, cases(), args.image)
    elif args.command == "export":
        data = export(args.root or Path("experiments") / (base + "-001"), p)
        dump(path, data)
        print(verify(data, p))
    elif args.command == "replay":
        require(args.study == "witness", "public differential replay requires --study witness")
        data = read(path)
        verify(data, p)
        print(
            "Reproduced",
            replay_witness(data, p, args.image),
            "model-authored public checks on both learn submissions and the reference.",
        )
    else:
        print(verify(read(path), p))
