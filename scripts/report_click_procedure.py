"""Export the independent-learning GLM study and verify its public evidence without model calls."""

import argparse
import html
import json
import sqlite3
from contextlib import closing, contextmanager
from decimal import Decimal
from pathlib import Path

from evocoder.contracts import Settings, Skill, Task, digest, safe_path
from evocoder.evolution import Evolution
from evocoder.gate import gate_summary, skill_behavior
from evocoder.grounding import validate_summary
from evocoder.procedure_evidence import behavior_receipt, step_edits
from evocoder.store import Store

TERMINAL = {"COMPLETE", "EXHAUSTED", "FAILED"}
NAMES = {"pallets-click-3d029e03eb": "计数选项", "pallets-click-42e2821ba3": "Click #557"}
PHASES = {
    "learning": "父版独立学习",
    "learning_replay": "子版学习回放",
    "focus": "焦点开发对照",
    "other_dev": "其他开发题",
    "known_regression": "历史回归",
    "support": "支持回放",
    "holdout": "新题",
}
STATUS = {
    "not_run": "未运行",
    "pending": "待检查",
    "blocked_by_focus": "焦点开发门禁提前结束",
    "blocked_by_full_gate": "完整门禁未通过",
    "complete": "已完成",
}


def check(ok, message):
    if not ok:
        raise ValueError(message)


def read(path):
    return json.loads(path.read_text())


class Archive(Store):
    """Reuse the store's hash-checked reads without initializing or writing database tables."""

    def __init__(self, path):
        self.db_path = path.resolve()

    @contextmanager
    def connect(self, write=False):
        check(not write, "read-only audit")
        wal = self.db_path.with_name(self.db_path.name + "-wal")
        suffix = "?mode=ro" if wal.exists() and wal.stat().st_size else "?mode=ro&immutable=1"
        with closing(sqlite3.connect(self.db_path.as_uri() + suffix, uri=True)) as db:
            db.row_factory = sqlite3.Row
            yield db


def billing(ledger, p, r):
    ids = r["billed_ids"]
    check(len(set(ids)) == len(ids), "duplicate billing id")
    totals = {}
    with ledger.connect() as db:
        for key in ids:
            rows = db.execute(
                "SELECT rowid,actual,state,reserved FROM calls WHERE run_id=? ORDER BY rowid", (key,)
            ).fetchall()
            check(
                rows and all(row["rowid"] > p["previous_billing_row"] for row in rows), "unbound billing rows"
            )
            totals[key] = {
                "calls": len(rows),
                "settled_nanos": sum(x["actual"] or 0 for x in rows),
                "reserved_nanos": sum(x["reserved"] for x in rows if x["state"] == "reserved"),
                "start": rows[0]["rowid"],
                "end": rows[-1]["rowid"],
            }
        if ids:
            all_rows = db.execute(
                "SELECT run_id FROM calls WHERE rowid>? AND rowid<=? ORDER BY rowid",
                (p["previous_billing_row"], max(x["end"] for x in totals.values())),
            )
            check(list(dict.fromkeys(x[0] for x in all_rows)) == ids, "interleaved or omitted billing ids")
    scale = Decimal(1000000000)
    actual = sum(x["settled_nanos"] for x in totals.values()) / scale
    reserved = sum(x["reserved_nanos"] for x in totals.values()) / scale
    check(
        Decimal(str(r["costs"]["cost_credits"])) == actual
        and Decimal(str(r["costs"]["reserved_credits"])) == reserved
        and r["costs"]["calls"] == sum(x["calls"] for x in totals.values()),
        "billing total mismatch",
    )
    check(
        abs(
            Decimal(str(r["budget_after"]["spent_credits"]))
            - Decimal(str(p["budget_before"]["spent_credits"]))
            - actual
        )
        <= Decimal("0.000001"),
        "billing ledger delta mismatch",
    )
    return totals


def features(receipt):
    tests = receipt["solver_requested_public_tests"]
    paired = [x for x in tests if set(x["named_states"]) == {"enabled", "disabled"}]
    return {
        "named": set(receipt["public_nodes"]) == {"enabled", "disabled"},
        "registered": sorted(receipt["public_nodes"]),
        "requested_tests": len(tests),
        "per_state": {
            state: {v: sum(x["named_states"].get(state) == v for x in tests) for v in ("passed", "failed")}
            for state in ("enabled", "disabled")
        },
        "paired_checks": len(paired),
        "both_passed": sum(all(v == "passed" for v in x["named_states"].values()) for x in paired),
        "green_saves": len(receipt["saved_green"]),
        "changed_green_hashes": len(
            {x["patch_hash"] for x in receipt["saved_green"] if x["changed_from_input"]}
        ),
        "restores": len(receipt["restored_green"]),
        "changed_restores": sum(x["changed_workspace"] is True for x in receipt["restored_green"]),
        "unknown_before_restores": sum(x["changed_workspace"] is None for x in receipt["restored_green"]),
        "failed_restores": len(receipt["failed_restores"]),
    }


def check_evidence(learning, eligible, params):
    refs = {r["run_id"]: r for r in learning}
    check(len(eligible) == len(set(eligible)) and set(eligible) <= set(refs), "unknown learning evidence")
    labels = {}
    for key in eligible:
        t = refs[key]
        f = t["execution_feedback"]
        check(
            t["task_id"] in params["learn_ids"]
            and t["applied_skill"] == params["parent"]
            and t["route"] == "apply"
            and t["eligible_for_revision"],
            "evidence not from parent learning",
        )
        if (
            t["state"] == "EXHAUSTED"
            and not t["passed"]
            and (f["step_budget_exhausted"] or f["command_budget_exhausted"])
        ):
            labels[key] = "终态耗尽"
        elif (
            t["state"] == "FAILED"
            and not t["passed"]
            and (f["post_edit_public_failure_sequences"] or f["learn_submission_failed_independent_check"])
        ):
            labels[key] = "终态失败"
        elif t["state"] == "COMPLETE" and t["passed"] and f["post_edit_public_failure_sequences"]:
            labels[key] = "修改后公开测试曾失败，最终通过"
        else:
            raise ValueError("learning reference has no qualifying failure")
    if any(refs[k]["passed"] for k in eligible):
        check(
            all(refs[k]["passed"] for k in eligible)
            and all(t["state"] == "COMPLETE" or not t["eligible_for_revision"] for t in learning)
            and {t["repeat"] for t in learning} == {0, 1},
            "premature intermediate-failure fallback",
        )
    return labels


def check_schedule(p, r, support):
    check(set(p["focus"]) == set(NAMES) and len(p["focus"]) == 2 and len(p["dev"]) == 3, "unknown focus gate")
    expected = [(k, i) for i in range(2) for k in p["focus"]]
    seen = [(x["task_id"], x["repeat"]) for x in r["rows"]]
    check(seen == expected[: len(seen)], "missing, repeated, or out-of-order focus cells")
    full, hold = r["gate"], r["holdout"]
    if full["rows"]:
        check(full["rows"][: len(seen)] == r["rows"], "focus rows changed at full gate")
    if not r.get("focus_gate"):
        check(not full["rows"] and not hold["rows"], "missing focus gate")
        if r["completed"]:
            check(not r.get("candidate") and not seen, "completed candidate lacks focus gate")
        return
    focus = gate_summary(r["rows"], 2)
    check(seen == expected and focus == r["focus_gate"], "focus gate differs from scored runs")
    possible = (
        not focus["regressions"]
        and not focus["infrastructure_errors"]
        and len(focus["repeat_confirmed_improved_groups"]) + len(set(p["dev"]) - set(p["focus"])) >= 2
    )
    if not possible:
        check(
            not full["rows"] and not hold["rows"] and full["status"] == hold["status"] == "blocked_by_focus",
            "blocked focus gate touched additional tasks",
        )
        return
    if full["rows"]:
        seq = [(x["evaluation_role"], x["task_id"], x["repeat"]) for x in full["rows"][len(seen) :]]
        expected = [
            (role, k, rep)
            for role, tasks, repeats in (
                ("other_dev", [k for k in p["dev"] if k not in p["focus"]], 2),
                ("known_regression", p["sentinels"], 2),
                ("support", sorted(support), 1),
            )
            for rep in range(repeats)
            for k in tasks
        ]
        check(seq == expected[: len(seq)], "incomplete or repeated full-gate cell")
        if full["status"] == "complete":
            check(
                seq == expected and all(full[k] == v for k, v in gate_summary(full["rows"], 2).items()),
                "incorrect full gate",
            )
    if r["completed"]:
        check(full["status"] == "complete", "missing complete full gate")
    if hold["rows"] or r["completed"] and full["gate"]["passed"]:
        expected = [(k, i) for i in range(2) for k in p["holdouts"]]
        got = [(x["task_id"], x["repeat"]) for x in hold["rows"]]
        check(
            full["gate"]["passed"]
            and got == expected[: len(got)]
            and (not r["completed"] or got == expected and hold["status"] == "complete"),
            "premature or incomplete holdout",
        )
    if r["completed"] and not full["gate"]["passed"]:
        check(hold["status"] == "blocked_by_full_gate", "missing held-out gate block")


def validate_learning_probe(probe, protocol, archived_run, events, task, reference):
    key, tid = probe["run_id"], probe["task_id"]
    expected_sources = {
        "original": digest({k: task.files[k] for k in task.editable}),
        "candidate": archived_run["result"]["patch"],
        "reference": digest(reference),
    }
    check(
        probe["schema"] == 1
        and probe["model_calls"] == 0
        and probe["post_hoc"] is True
        and probe["used_for_gate"] is False
        and probe["study_protocol_hash"] == digest(protocol)
        and tid == task.id
        and probe["split"] == task.split == "learn"
        and archived_run["purpose"] == "evaluate"
        and archived_run["id"] == key
        and archived_run["task_id"] == tid
        and archived_run["task_hash"] == probe["task_hash"] == digest(task.model_dump())
        and probe["run_result_hash"] == digest(archived_run["result"])
        and probe["source_hashes"] == expected_sources
        and probe["reference_hash"] == expected_sources["reference"]
        and probe["patch_hash"] == expected_sources["candidate"],
        "unbound learning patch probe",
    )
    by_kind = {e["kind"]: e for e in events if e["kind"] in {"PATCH", "PUBLIC_VERIFY", "GRADE"}}
    check(
        probe["evidence_refs"] == {k: e["seq"] for k, e in by_kind.items()}
        and by_kind["PATCH"]["payload"]["artifact"] == probe["patch_hash"]
        and by_kind["PUBLIC_VERIFY"]["payload"]["observation"]["exit_code"]
        == probe["recorded_public_exit"]
        == 0
        and by_kind["GRADE"]["payload"]["passed"]
        is probe["recorded_grade"]
        is archived_run["result"]["passed"]
        is False,
        "learning patch probe does not prove public pass and independent failure",
    )
    mismatches = []
    fields = {"stripped", "visible_length", "color_on_help", "color_off_help", "exit_codes"}
    for name, case in probe["cases"].items():
        for version in ("original", "candidate", "reference"):
            result = case[version]
            check(
                set(result) == fields
                and result["exit_codes"] == [0, 0]
                and len(result["stripped"]) == result["visible_length"],
                "invalid public observations in learning patch probe",
            )
        differing = {k for k in fields if case["candidate"][k] != case["reference"][k]}
        check(
            differing == set(case["differing_fields"]) and case["matches_reference"] is (not differing),
            "learning patch probe comparisons differ from recorded outputs",
        )
        if differing:
            mismatches.append(name)
    check(
        mismatches == probe["mismatched_cases"]
        and len(mismatches) == probe["mismatched_case_count"]
        and len(probe["cases"]) - len(mismatches) == probe["matched_case_count"],
        "learning patch probe totals differ",
    )


def public_events(events):
    kinds = {
        "START",
        "ACTION",
        "ACTION_REJECTED",
        "OBSERVATION",
        "SKILL_ROUTE",
        "PATCH",
        "PUBLIC_VERIFY",
        "PUBLIC_CHECKPOINT",
        "CHECKPOINT_TESTED",
        "CHECKPOINT_RESTORED",
        "CHECKPOINT_RESTORE_FAILED",
    }
    output = [e for e in events if e["kind"] in kinds]
    for e in events:
        if e["kind"] == "GRADE":
            output.append({"seq": e["seq"], "kind": "GRADE", "payload": {"passed": e["payload"]["passed"]}})
    return sorted(output, key=lambda e: e["seq"])


def audit_fixed(root, p, result, normal_runs):
    plan = read(root / "fixed-plan.json")
    if digest(p) != plan["protocol"] or any(k.split("/")[0] not in {"learning", "learning_replay"}
                                             for k in plan["earlier_slots"]):
        raise ValueError("invalid fixed-route registration")
    folder = root.with_name(root.name + "-fixed")
    if not (folder / "fixed-result.json").is_file():
        return {"fixed-plan.json": plan}
    fixed = read(folder / "fixed-result.json")
    spec = {k: fixed[k] for k in ("settings", "normal_result", "plan")}
    check(spec == {"settings": p["settings"], "normal_result": digest(result), "plan": digest(plan)}
          and read(folder / "start.json") == {"spec": spec, "budget_before": fixed["before"]}
          and not fixed["published"] and not fixed["holdout_rows"], "fixed diagnostic provenance mismatch")
    earliest = min(normal_runs[r[k + "_run"]]["created"] for r in result["rows"] for k in ("baseline", "candidate"))
    check(plan["registered_at"] < earliest, "fixed registration followed exposure of a focus task")
    slots = read(folder / "slots.json")
    store = Archive(folder / "seed/evocoder.sqlite3")
    trace = {}
    members = (p["parent"], result["candidate"])
    check((fixed["parent"], fixed["candidate"]) == members and len(fixed["rows"]) == len(plan["cells"]) == 4,
          "incomplete fixed comparison")
    for cell, row in zip(plan["cells"], fixed["rows"], strict=True):
        check((cell["task"], cell["repeat"]) == (row["task_id"], row["repeat"])
              and cell["task"] in p["focus"] and not row["infrastructure_error"], "wrong fixed diagnostic cell")
        times = []
        for arm, skill in zip(("baseline", "candidate"), members, strict=True):
            k = row[arm + "_run"]
            slot = f"fixed_apply/{row['task_id']}/{row['repeat']}/{skill}"
            attempts = slots[slot]
            check(attempts[-1]["run_id"] == k and all(a["run_id"] == digest([digest(spec), slot, i])[:32]
                  for i, a in enumerate(attempts)), "incorrect fixed trial identity")
            run = store.run(k)
            ev = store.events(k)
            start, = [e["payload"] for e in ev if e["kind"] == "START"]
            check(run["config"] == p["settings"] and run["purpose"] == "evaluate" and run["split"] == "dev"
                  and run["skill_id"] == skill == row[arm + "_applied_skill"] and row[arm + "_route"] == "apply"
                  and start["fixed_route"] == "apply" and start["requested_skill_ids"] == [skill]
                  and run["state"] in TERMINAL and run["task_hash"] == row["task_hash"]
                  and run["task_id"] == cell["task"] and run["result"]["image_id"] == p["settings"]["image"]
                  and row[arm + "_pass"] is run["result"]["passed"], "fixed arm differs from actual injection/score")
            trace[k] = {"slot": slot, "state": run["state"], "passed": run["result"]["passed"],
                        "events": public_events(ev)}
            times.append(run["created"])
        check((times[0] > times[1]) == cell["reverse"], "unexpected fixed arm order")
    ids = [a["run_id"] for attempts in slots.values() for a in attempts]
    check(ids == fixed["run_ids"] and len(ids) == len(set(ids)), "unexpected diagnostic billing ids")
    bills = Archive(Path(p["settings"]["state_dir"]) / "evocoder.sqlite3")
    with bills.connect() as db:
        rows = [(b["actual"] or 0, b["reserved"] if b["state"] == "reserved" else 0) for k in ids
                for b in db.execute("SELECT actual,reserved,state FROM calls WHERE run_id=?", (k,))]
    check(len(rows) == fixed["costs"]["calls"] and all(abs(sum(x[i] for x in rows) / 1e9 - fixed["costs"][field]) < 1e-6
          for i, field in enumerate(("cost_credits", "reserved_credits"))), "incorrect fixed diagnostic billing")
    return {"fixed-plan.json": plan, "fixed-result.json": fixed, "fixed-traces.json": trace}


def audit_check_repair(root, p, result):
    from repair_click_authored_checks import subjects
    from verify_authored_cli_checks import verdict
    from verify_cli_reach import indices
    dest = root / "check-repair"
    plan, r = read(dest / "plan.json"), read(dest / "result.json")
    check(plan["parent_study"] == r["main"] == digest(result) and plan["subjects"] == subjects(root)
          and r["plan"] == digest(plan) and not r["holdout_tasks"] and not r["registered_skills"]
          and not r["used_for_promotion"] and r["image"] == p["settings"]["image"]
          and r["model"] == p["settings"]["model"], "directed repair provenance differs")
    st = Archive(dest / "state/evocoder.sqlite3")
    ids = []
    for sub, trial in zip(plan["subjects"], r["trials"], strict=True):
        check(all(sub[k] == trial[k] for k in ("role", "task", "run", "seq", "patch"))
              and 1 <= len(trial["attempts"]) <= plan["max_attempts"], "incorrect directed repair scope")
        for i, a in enumerate(trial["attempts"]):
            k = digest([digest(plan), trial["subject"], i])[:32]
            reply, = [e["payload"] for e in st.events(k) if e["kind"] == "CHECK_REPAIR_REPLY"]
            check(k == a["id"] and reply["source"] == a["source"] and a["source_hash"] == digest(a["source"]),
                  "directed script differs from the actual model reply")
            ids.append(k)
            if a["source"]:
                hit = indices(a["original_calls"], a["reference_calls"], a["mutant_calls"])
                status = verdict(a["original_action"], a["reference"], a["off_mutant"], a["exit"], r["image"])
                check(hit == a["matched_call_indices"] and a["caught_disabled"] ==
                      bool(hit and a["raw_execution_matches"] and status == "detects_disabled_regression"),
                      "unsupported directed diagnostic result")
    bills = Archive(Path(p["settings"]["state_dir"]) / "evocoder.sqlite3")
    with bills.connect() as db:
        entries = [b for k in ids for b in db.execute("SELECT actual,state,reserved FROM calls WHERE run_id=?", (k,))]
    check(len(entries) == r["costs"]["calls"] and abs(sum(b["actual"] or 0 for b in entries)/1e9 - r["costs"]["cost_credits"]) < 1e-6
          and abs(sum(b["reserved"] for b in entries if b["state"] == "reserved")/1e9 - r["costs"]["reserved_credits"]) < 1e-6,
          "directed repair billing differs")
    outputs = {"check-repair-plan.json": plan, "check-repair-result.json": r}
    covered = dest / "coverage-feedback"
    if (covered / "result.json").is_file():
        from check_cli_coverage_feedback import choice_modes
        from repair_click_authored_checks import has_assertion
        cp, cr = read(covered / "plan.json"), read(covered / "result.json")
        check(cp["main"] == cr["main"] == digest(result) and cp["earlier"] == digest(r) and cp["earlier_plan"] == digest(plan)
              and cr["plan"] == digest(cp) and cp["actions_per_subject"] == 1 and not cr["used_for_promotion"]
              and not cr["registered_skills"] and not cr["holdout_tasks"] and cr["image"] == r["image"],
              "coverage follow-up provenance mismatch")
        records = {a["id"]: a for t in r["trials"] for a in t["attempts"]}
        cstore = Archive(covered / "state/evocoder.sqlite3")
        for i, (sub, a) in enumerate(zip(cp["subjects"], cr["rows"], strict=True)):
            earlier = records[sub["prior_attempt"]]
            check(sub["measured_modes"] == [sub["public_mode"]] == choice_modes(earlier["original_calls"])
                  and sub["missing_mode"] is not sub["public_mode"] and sub["prior_source"] == earlier["source"]
                  and a["id"] == digest([digest(cp), i])[:32] and a["patch"] == sub["patch"] and a["run"] == sub["run"]
                  and a["task"] == sub["task"] and a["task"] in p["learn_ids"], "unsupported measured coverage gap")
            reply, = [e["payload"] for e in cstore.events(a["id"]) if e["kind"] == "CHECK_REPAIR_REPLY"]
            check(reply["source"] == a["source"] and digest(a["source"]) == a["source_hash"], "unbound new check")
            if a["source"]:
                new_state = a["raw_execution_matches"] and choice_modes(a["original_calls"]) == [sub["missing_mode"]] and has_assertion(a["source"])
                hit = indices(a["original_calls"], a["reference_calls"], a["mutant_calls"])
                evidence = verdict(a["original_action"], a["reference"], a["off_mutant"], a["exit"], cr["image"])
                check(new_state == a["isolated_new_state"] and hit == a["matched_call_indices"]
                      and a["caught_disabled"] == bool(new_state and hit and evidence == "detects_disabled_regression"),
                      "coverage follow-up outcome differs from run")
        with bills.connect() as db:
            extra = [b for a in cr["rows"] for b in db.execute("SELECT actual,state,reserved FROM calls WHERE run_id=?", (a["id"],))]
        check(len(extra) == cr["costs"]["calls"] and abs(sum(b["actual"] or 0 for b in extra)/1e9 - cr["costs"]["cost_credits"]) < 1e-6
              and abs(sum(b["reserved"] for b in extra if b["state"] == "reserved")/1e9 - cr["costs"]["reserved_credits"]) < 1e-6,
              "coverage follow-up billing differs")
        outputs.update({"coverage-feedback-plan.json": cp, "coverage-feedback-result.json": cr})
    return outputs


def audit(root):
    p, r = (read(root / f) for f in ("protocol.json", "result.json"))
    missing = p["claim"] == "missing_cli_state_revision"
    policy = "bound_procedure" if missing else "learn_procedure_feedback"
    check(
        r["protocol"] == p
        and p["claim"] in {"independent_learn_procedure_revision", "missing_cli_state_revision"}
        and "api_key" not in p["settings"]
        and p["settings"]["public_checkpoints"] is True
        and read(root / "started.json") == {"protocol_hash": digest(p)},
        "unbound protocol/start",
    )
    task_entries = {}
    for prefix in ("learn_", ""):
        path = Path(p[prefix + "manifest"])
        data = read(path)
        check(digest(data) == p["learn_hash" if prefix else "manifest_hash"], "unbound task manifest")
        for entry in data["tasks"]:
            check(entry["id"] not in task_entries, "overlapping task ids")
            task_entries[entry["id"]] = path.parent / safe_path(entry["path"]), entry
    check(
        digest({f.name: f.read_text() for f in (root / "source").glob("*.py")}) == p["source_hash"]
        and digest(read(root / "drivers.json")) == p["drivers_hash"],
        "frozen source hashes differ",
    )
    for name, expected in p.get("support_manifests", {}).items():
        path = Path(name)
        historical = read(path)
        check(digest(historical) == expected, "changed support manifest")
        for entry in historical["tasks"]:
            if entry["id"] in p["prior_support"]:
                check(entry["id"] not in task_entries, "duplicate support snapshot")
                task_entries[entry["id"]] = path.parent / safe_path(entry["path"]), entry
    proof = read(Path(p["learn_manifest"]).parent / "verification.json")
    check(
        digest(proof) == p["learn_verification_hash"] and proof["passed"] and proof["model_calls"] == 0,
        "unverified learning task setup",
    )
    store = Archive(root / "seed/evocoder.sqlite3")
    bills = billing(Archive(Path(p["settings"]["state_dir"]) / "evocoder.sqlite3"), p, r)
    outputs = {"protocol.json": p, "result.json": r, "started.json": read(root / "started.json")}
    independent = read(Path(p["learn_manifest"]).parent / "independence.json")
    check(
        digest(independent) == p["independence_hash"]
        and independent["passed"]
        and independent["manifest_hash"] == p["learn_hash"]
        and independent["model_calls"] == 0,
        "learning independence receipt mismatch",
    )
    outputs["independence.json"] = independent
    slots = read(root / "slots.json")
    outputs["slots.json"] = slots
    runs = {}
    events = {}
    slots_by_run = {}
    for slot, attempts in slots.items():
        phase, task_id, repeat, skill = slot.split("/")
        check(
            phase in PHASES
            and task_id in task_entries
            and repeat in {"0", "1"}
            and skill in {p["parent"], r["candidate"]},
            "invalid slot coordinates",
        )
        if phase == "holdout":
            check(r["gate"].get("gate", {}).get("passed"), "holdout slot before gate")
        if phase in {"other_dev", "known_regression", "support"}:
            check(
                r["focus_gate"] and r["gate"]["status"] != "blocked_by_focus",
                "full-gate slot before focus gate",
            )
        for i, attempt in enumerate(attempts):
            key = attempt["run_id"]
            check(key == digest([digest(p), slot, i])[:32] and key not in runs, "slot id mismatch")
            run = store.run(key)
            ev = store.events(key)
            source = task_entries[task_id][1]
            check(
                run["task_id"] == task_id
                and run["task_hash"] == source["hash"]
                and run["split"] == source["split"]
                and run["config"] == p["settings"]
                and run["mode"] == "live"
                and run["purpose"] == ("solve" if phase == "learning" else "evaluate"),
                "run differs from slot",
            )
            (start,) = [x["payload"] for x in ev if x["kind"] == "START"]
            check(
                start["requested_skill_ids"] == [skill]
                and (
                    start.get("fixed_route")
                    == ("apply" if phase in {"learning", "learning_replay"} else None)
                ),
                "incorrect solver treatment",
            )
            runs[key], events[key], slots_by_run[key] = run, ev, (phase, task_id, int(repeat), skill)
            outputs["traces/" + key + ".json"] = {
                "run_id": key,
                "slot": slot,
                "state": run["state"],
                "passed": run["result"]["passed"],
                "events": public_events(ev),
            }
    check(
        set(r["run_ids"]) <= set(runs) and set(runs) & set(bills) == set(r["run_ids"]),
        "unknown billed solver",
    )
    for k in set(bills) - set(runs):
        (start,) = [e["payload"] for e in store.events(k) if e["kind"] == "PROPOSAL_START"]
        check(
            start["parent_id"] == p["parent"] and start["proposal_policy"] == policy,
            "unrelated billing",
        )
    summaries = {}
    task_cache = {}

    def observe(key, supplied):
        run = runs[key]
        task_id = run["task_id"]
        if task_id not in task_cache:
            task_cache[task_id] = Task.model_validate(read(task_entries[task_id][0]))
        receipt = behavior_receipt(store, task_cache[task_id], key)
        if supplied is not None:
            check(
                receipt == read(root / "behavior" / (key + ".json")) and digest(receipt) == supplied,
                "behavior receipt mismatch",
            )
        summaries[key] = features(receipt)
        outputs["behavior/" + key + ".json"] = receipt

    learn = r["learning"]
    # Recompute feedback using archived public state and the independent grader's one-bit result.
    evo = Evolution(Settings(_env_file=None), store, model=object(), feedback_policy="learn_outcome", proposal_policy=policy)
    expected_learning = [(k, i) for i in range(2) for k in p["learn_ids"]]
    check(
        [(x["task_id"], x["repeat"]) for x in learn] == expected_learning[: len(learn)],
        "incomplete learning schedule",
    )
    check(
        [x["task_id"] for x in r["learning_replay"]] == p["learn_ids"][: len(r["learning_replay"])],
        "invalid learning replay schedule",
    )
    if r.get("eligible_runs") or r["completed"]:
        check(len(learn) in {len(p["learn_ids"]), 2 * len(p["learn_ids"])}, "incomplete learning wave")
    if r["completed"] and r["candidate"]:
        check(len(r["learning_replay"]) == len(p["learn_ids"]), "missing learning replay")
    for trial in learn:
        k = trial["run_id"]
        feedback = evo.revision_feedback(runs[k], events[k])
        check(
            feedback == trial["execution_feedback"]
            and trial["applied_skill"] == runs[k]["skill_id"] == p["parent"]
            and trial["eligible_for_revision"] is evo.qualifies({"execution_feedback": feedback}),
            "learning evidence differs from actual solver feedback",
        )
    for trial in [*learn, *r["learning_replay"]]:
        k = trial["run_id"]
        phase = "learning" if "eligible_for_revision" in trial else "learning_replay"
        check(
            slots_by_run[k]
            == (
                phase,
                trial["task_id"],
                trial.get("repeat", 0),
                p["parent"] if phase == "learning" else r["candidate"],
            )
            and runs[k]["state"] == trial["state"]
            and runs[k]["result"]["passed"] is trial["passed"],
            "incorrect learning trial",
        )
        observe(k, trial["behavior_hash"])
    eligible = r.get("eligible_runs", [])
    labels = check_evidence(learn, eligible, p)
    if r["completed"] or r.get("eligible_runs"):
        terminal = [
            x["run_id"] for x in learn if x["eligible_for_revision"] and x["state"] in {"FAILED", "EXHAUSTED"}
        ]
        recovered = [x["run_id"] for x in learn if x["eligible_for_revision"] and x["state"] == "COMPLETE"]
        check(eligible == (terminal or recovered), "frozen failures differ from eligibility policy")
    support = set(p["prior_support"])
    edits = []
    if r["candidate"]:
        rev = read(root / "revision.json")
        child, parent = rev["candidate"], rev["parent"]
        check(
            digest(child) == rev["candidate_id"] == r["candidate"]
            and child == store.get_artifact(r["candidate"])
            and digest(parent) == child["parent"] == rev["parent_id"] == p["parent"]
            and parent == store.get_artifact(p["parent"]),
            "version body/hash mismatch",
        )
        pool = store.get_artifact(rev["frozen_evidence"])
        check(
            rev["eligible_ids"] == eligible == [e["run_id"] for e in pool["episodes"]]
            and pool["parent_id"] == p["parent"]
            and set(child["evidence_refs"]) <= set(eligible),
            "unbound proposal evidence",
        )
        support.update(runs[k]["task_id"] for k in eligible)
        with store.connect() as db:
            rows = db.execute("SELECT run_id,payload FROM events WHERE kind='SUMMARY_VERIFIED'")
            (verified,) = [
                (x[0], json.loads(x[1])) for x in rows if json.loads(x[1]).get("candidate") == r["candidate"]
            ]
        proposal_id, marker = verified
        summary = store.get_artifact(marker["artifact"])
        edits = step_edits(parent["steps"], child["steps"])
        check(
            proposal_id in bills
            and summary["parent"] == p["parent"]
            and summary["candidate"] == r["candidate"]
            and summary["step_edits"] == edits
            and edits
            and summary["frozen_evidence"] == rev["frozen_evidence"]
            and summary["evidence_refs"] == child["evidence_refs"]
            and summary["summary_hash"] == digest(child["change_summary"]),
            "proposal receipt mismatch",
        )
        catalog = store.get_artifact(summary["catalog"])
        validate_summary(child["change_summary"], child["evidence_refs"], catalog)
        if missing:
            from evocoder.step_contract import bind_revision
            program = store.get_artifact(summary["step_program"])
            rendered, witnesses = bind_revision(program, Skill.model_validate(parent), catalog)
            check(rendered.model_copy(update={"change_summary": child["change_summary"]}).model_dump() == child
                  and witnesses == summary["fact_witnesses"], "materialized steps or historical facts mismatch")
            outputs["step-program.json"] = program
        outputs.update(
            {"revision.json": rev, "summary-receipt.json": summary, "learning-receipts.json": catalog}
        )
    check_schedule(p, r, support)
    pairs = [(x, "focus") for x in r["rows"]]
    pairs += [(x, x["evaluation_role"]) for x in r["gate"]["rows"][len(r["rows"]) :]]
    pairs += [(x, "holdout") for x in r["holdout"]["rows"]]
    compared = set()
    for row, phase in pairs:
        task = row["task_id"]
        meta = task_entries[task][1]
        check(
            row["source_group"] == meta["source_group"]
            and row["split"] == meta["split"]
            and row["policy"] == "normal"
            and row["task_hash"] == meta["hash"]
            and row["skill_behavior"] == skill_behavior(row, r["candidate"], {p["parent"]}),
            "unbound paired evaluation",
        )
        ids = []
        for arm, skill in (("baseline", p["parent"]), ("candidate", r["candidate"])):
            key = row[arm + "_run"]
            run = runs[key]
            check(
                key not in compared
                and slots_by_run[key] == (phase, task, row["repeat"], skill)
                and run["state"] in TERMINAL
                and run["skill_id"] == row[arm + "_applied_skill"]
                and run["result"]["routing"]["decision"] == row[arm + "_route"]
                and run["result"]["image_id"] == p["settings"]["image"]
                and run["result"]["passed"] is row[arm + "_pass"],
                "paired arm differs from live run",
            )
            compared.add(key)
            ids.append(key)
            observe(key, row.get(arm + "_behavior_hash"))
        check(not row["infrastructure_error"], "infra error submitted for grading")
        order = (
            ["baseline", "candidate"]
            if runs[ids[0]]["created"] < runs[ids[1]]["created"]
            else ["candidate", "baseline"]
        )
        check(row["arm_order"] == order, "reported arm order differs from timestamps")
        if phase in {"focus", "holdout"}:
            seq = p["focus"] if phase == "focus" else p["holdouts"]
            reverse = bool((row["repeat"] + seq.index(task)) % 2)
            check(
                order == (["candidate", "baseline"] if reverse else ["baseline", "candidate"]),
                "arm order differs from protocol",
            )
    if r["completed"]:
        terminal = {k for k, v in runs.items() if v["state"] in TERMINAL}
        check(
            terminal == compared | {x["run_id"] for x in learn + r["learning_replay"]},
            "unaccounted terminal run",
        )
    outputs["learn-verification.json"] = {
        "manifest_hash": proof["manifest_hash"],
        "image_id": proof["image_id"],
        "model_calls": proof["model_calls"],
        "passed": proof["passed"],
        "rows": [
            {
                "id": x["id"],
                "task_hash": x["task_hash"],
                "passed": x["passed"],
                "checks": {k: {"exit_code": v["exit_code"]} for k, v in x["checks"].items()},
            }
            for x in proof["rows"]
        ],
    }
    # Include independent setup outcomes only; hidden test source/output stays outside the export.
    if (root / "learning-patch-probe.json").is_file():
        probe = read(root / "learning-patch-probe.json")
        run_id = probe["run_id"]
        check(
            slots_by_run[run_id][0] == "learning_replay" and runs[run_id]["skill_id"] == r["candidate"],
            "learning patch probe must describe a child learning replay",
        )
        tid = probe["task_id"]
        ctrl = read(Path(p["learn_manifest"]).parent / "controller" / (safe_path(tid) + ".json"))
        validate_learning_probe(probe, p, runs[run_id], events[run_id], task_cache[tid], ctrl["reference"])
        outputs["learning-patch-probe.json"] = probe
    if (root / "step-claim-audit.json").is_file():
        from audit_click_step_claim import audit as reconstruct_step_claim

        claim = read(root / "step-claim-audit.json")
        check(
            claim == reconstruct_step_claim(root)
            and claim["candidate"] == r["candidate"]
            and claim["frozen_evidence"] == outputs["revision.json"]["frozen_evidence"]
            and set(claim["evidence_refs"]) <= set(labels),
            "step assertion audit differs from cited learning executions",
        )
        outputs["step-claim-audit.json"] = claim
    probes = root / "patch-probes.json"
    if probes.is_file():
        prob = read(probes)
        check(
            prob["result_hash"] == digest(r)
            and prob["model_calls"] == 0
            and set(prob["task_hashes"]) == set(p["focus"])
            and set(t["run_id"] for t in prob["trials"]) == {k for k in runs if slots_by_run[k][0] == "focus"}
            and len(prob["trials"]) == len(set(t["run_id"] for t in prob["trials"]))
            and prob["runtime_image"] == p["settings"]["image"]
            and prob["phase"] == r["phase"]
            and prob["completed"] is r["completed"]
            and all(
                t["run_id"] in runs
                and t["paired"] is (t["run_id"] in compared)
                and t["task_id"] in p["focus"]
                and t["patch_hash"] == runs[t["run_id"]]["result"]["patch"]
                for t in prob["trials"]
            ),
            "unbound post-hoc patch diagnostics",
        )
        outputs["patch-probes.json"] = prob
    replay_path = root / "checkpoint-replay.json"
    if replay_path.is_file():
        replay = read(replay_path)
        check(
            replay["model_calls"] == 0
            and replay["protocol_hash"] == digest(p)
            and replay["result_hash"] == digest(r),
            "unbound offline checkpoint replay",
        )
        if replay["status"] == "verified":
            source = replay["source"]
            run = source["run_id"]
            green = replay["green_hash"]
            receipts = outputs[f"behavior/{run}.json"]
            stages = replay["stages"]
            check(
                source["phase"] in {"learning", "learning_replay"}
                and source["task_id"] in p["learn_ids"]
                and source["task_id"] == runs[run]["task_id"]
                and source["skill_id"] == runs[run]["skill_id"]
                and source["behavior_hash"] == digest(receipts)
                and source["checkpoint_event"] in events[run]
                and source["checkpoint_event"]["kind"] == "PUBLIC_CHECKPOINT"
                and source["checkpoint_event"]["payload"]["patch_hash"] == green
                and [x["workspace_hash"] for x in stages] == [green, replay["original_hash"], green]
                and green != replay["original_hash"]
                and [x["exit_code"] for x in stages] == [0, 1, 0]
                and [x["stage"] for x in stages] == ["green", "original_bug", "restored"]
                and all(
                    stages[i]["named_states"] == {"enabled": "passed", "disabled": "passed"} for i in (0, 2)
                )
                and "failed" in stages[1]["named_states"].values()
                and all(x["image_id"] == p["settings"]["image"] and not x["timeout"] for x in stages),
                "invalid green/broken/restored checkpoint replay",
            )
        else:
            check(
                replay["status"] == "no_changed_green_learning_checkpoint" and replay["source"] is None,
                "unknown checkpoint replay result",
            )
        outputs["checkpoint-replay.json"] = replay
    if missing:
        from evocoder.runtime import validation
        from verify_step_admission import verify as verify_admission
        from verify_authored_cli_checks import audit as audit_authorship
        admission = read(root / "step-admission.json")
        controls = read(root / "cli-controls.json")
        check(digest(admission) == p["admission_hash"] and admission == verify_admission(Path(p["origin"]))
              and admission["candidate"] == p["parent"] and digest(controls) == p["controls_hash"],
              "unbound admission or control cases")
        outputs["step-admission.json"], outputs["cli-controls.json"] = admission, controls
        if (root / "fixed-plan.json").is_file():
            outputs.update(audit_fixed(root, p, r, runs))
        if (root / "check-repair/result.json").is_file():
            outputs.update(audit_check_repair(root, p, r))
        if r["completed"]:
            postmortem = []
            for trial in r["learning"]:
                key = trial["run_id"]
                patch_id = runs[key]["result"]["patch"]
                if trial["state"] != "FAILED" or not patch_id:
                    continue
                task = task_cache[trial["task_id"]]
                check(task.split == "learn", "postmortem requires a learning snapshot")
                options = Settings(_env_file=None, image=p["settings"]["image"])
                replay = validation({**task.files, **store.get_artifact(patch_id), **task.grader_files},
                                    [*task.grader_command, "-rA"], options, "learn-postmortem")
                checks = [s.split(" - ", 1)[0] for s in replay["output"].splitlines()
                          if s.startswith(("PASSED ", "FAILED "))]
                check(replay["exit_code"] == 1 and not replay["timeout"] and replay["image_id"] == options.image
                      and any(s.startswith("FAILED ") for s in checks), "failed learn submission did not reproduce")
                postmortem.append({"run": key, "task": task.id, "patch": patch_id, "tests": checks,
                                   "image": options.image, "exit": replay["exit_code"]})
            outputs["learn-postmortem.json"] = {"model_calls": 0, "supplied_to_proposer": False, "runs": postmortem}
            observed = audit_authorship(root)
            if (root / "authored-checks.json").is_file():
                check(read(root / "authored-checks.json") == observed, "authored-check replays disagree")
            outputs["authored-checks.json"] = observed
            if (root / "reach-plan.json").exists():
                from verify_cli_reach import audit as audit_reach
                reach_plan = read(root / "reach-plan.json")
                check(all(reach_plan["registered_at"] < runs[row[arm + "_run"]]["created"]
                          for row in r["rows"] for arm in ("baseline", "candidate")),
                      "invocation reach criterion followed focus exposure")
                outputs["reach-plan.json"] = reach_plan
                outputs["cli-reach.json"] = audit_reach(root, observed)
                if (root / "reach-observations.json").is_file():
                    check(outputs["cli-reach.json"] == read(root / "reach-observations.json"), "reach replay differs")
    audit_log = {
        "protocol_hash": digest(p),
        "result_hash": digest(r),
        "model_calls_for_report": 0,
        "trial_count": len(runs),
        "paired_runs": len(compared),
        "eligible_learn_failures": labels,
        "step_edits": edits,
        "behavior": summaries,
        "billing": bills,
    }
    outputs["audit.json"] = audit_log
    return r, outputs


class Report:
    def __init__(self):
        self.html = []
        self.md = []

    def heading(self, title, level=2):
        self.html.append(f"<h{level}>{html.escape(title)}</h{level}>")
        self.md.append("#" * level + " " + title)

    def paragraph(self, value):
        self.html.append("<p>" + html.escape(value) + "</p>")
        self.md.append(value)

    @staticmethod
    def cell(value):
        if isinstance(value, tuple):
            title, href = value
            return (
                f'<a href="{html.escape(href, quote=True)}">{html.escape(title)}</a>',
                f"[{title}]({href})",
            )
        raw = str(value)
        return html.escape(raw), raw.replace("|", "\\|").replace("\n", " ")

    def table(self, titles, rows):
        if not rows:
            return
        rendered = []
        lines = []
        for i, row in enumerate([titles, *rows]):
            tag = "th" if i == 0 else "td"
            cells = [self.cell(v) for v in row]
            rendered.append("<tr>" + "".join(f"<{tag}>{h}</{tag}>" for h, _ in cells) + "</tr>")
            lines.append("| " + " | ".join(m for _, m in cells) + " |")
            if not i:
                lines.append("|" + "---|" * len(titles))
        self.html.append("<div><table>" + "".join(rendered) + "</table></div>")
        self.md.append("\n".join(lines))

    def write(self, output):
        style = "body{font:16px/1.65 system-ui;max-width:1200px;margin:2rem auto;padding:0 1rem;color:#182634}div{overflow:auto}table{border-collapse:collapse}td,th{padding:.35rem .55rem;border:1px solid #c8d0d5}th{background:#eef4f7}h2{margin-top:1.8em}a{color:#23567d}"
        (output / "report.html").write_text(
            '<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>Click 技能步骤修订实验</title><style>'
            + style
            + "</style>"
            + "\n".join(self.html)
            + "</html>"
        )
        (output / "report.md").write_text("\n\n".join(self.md) + "\n")


def render(output, r, bundle):
    doc = Report()
    p = r["protocol"]
    log = bundle["audit.json"]
    summaries = log["behavior"]
    refs = log["eligible_learn_failures"]
    cited = set(bundle.get("revision.json", {}).get("candidate", {}).get("evidence_refs", []))
    doc.heading("Click 技能步骤修订实验", 1)
    doc.paragraph(
        f"GLM 父版 {p['parent'][:12]}，子版 {r['candidate'][:12] if r['candidate'] else '尚未生成'}。"
        f"本次记录 {len(r['learning'])} 次固定注入父技能的学习运行、{len(r['learning_replay'])} 次子版回放、"
        f"{len(r['rows'])} 对焦点开发结果和 {len(r['holdout']['rows'])} 对新题结果。"
    )
    if "step-admission.json" in bundle:
        admission = bundle["step-admission.json"]
        fact = admission["positive_control"]
        doc.heading("第 7 步的历史结论经过准入回归测试")
        doc.paragraph(f"上一版步骤声称同一次测试出现开启通过、关闭失败。第 {fact['observation_seq']} 条观察实测两态均失败。"
                      "三种输入经过同一个提案注册入口，核验器逐次检查临时库中新增的技能数。")
        doc.table(["输入", "结果", "新增技能"], [
            [name, "接纳" if x["accepted"] else "拒绝", x["skill_rows_added"]]
            for name, x in zip(("原始自由文本", "指定同次测试但状态不符", "同次测试与状态均相符"), admission["checks"])
        ])
        doc.heading("GLM 自己编写的关闭场景检查")
        doc.paragraph("每道学习题只提供一个开启用例。父子使用相同工具和行动预算，脚本由发起工具调用的模型编写。"
                      "核验要求真实调用本地 CliRunner、在上游修复通过、在仅改变关闭行为的变体上触发脚本自己的断言。"
                      "计数题中的对应边界是普通数值选项对显式范围的显示。")
        checks = bundle.get("authored-checks.json", {})
        if checks:
            reach = bundle.get("cli-reach.json", {}).get("runs", {})
            doc.table(["阶段", "任务", "重复", "版本", "独立评分", "可重放脚本", "到达关闭调用", "捕捉关闭回归"], [
                [PHASES[t["phase"]], NAMES.get(t["task_id"], t["task_id"]), t["repeat"] + 1,
                 "父" if t["skill"] == p["parent"] else "子", "通过" if t["passed"] else "失败",
                 t["authored_actions"], "已证实" if reach.get(t["run_id"], {}).get("reached_disabled") else "未证实",
                 "已证实" if reach.get(t["run_id"], {}).get("caught_disabled") else "未证实"] for t in checks["trials"]
            ])
            doc.paragraph("计数单位为运行。原动作还须复现退出码与原始输出哈希，并在当时的补丁上真正走到产生差异的同一次 CLI 调用。"
                          "单纯打印、只跑公开测试和触发无关异常均不计入关闭回归检测。"
                          "普通命令中无法可靠提取的脚本计入未分类，执行记录保存在逐次轨迹中。")
        doc.table(["证据"], [[("逐次脚本、真实执行与变体重放", "authored-checks.json")],
                             [("运行前冻结的正负对照", "cli-controls.json")],
                             [("原始第 7 步与准入回放", "step-admission.json")]])
        if bundle.get("learn-postmortem.json", {}).get("runs"):
            doc.heading("父版提交的补丁漏了哪些分支")
            doc.paragraph("子版冻结后，在父版提交的学习补丁上重新执行独立测试。提案模型当时只收到了独立评分的通过或失败。")
            doc.table(["学习题", "失败的测试节点", "本次重放通过数"], [
                [v["task"], " / ".join(x.removeprefix("FAILED ") for x in v["tests"] if x.startswith("FAILED ")),
                 sum(x.startswith("PASSED ") for x in v["tests"])] for v in bundle["learn-postmortem.json"]["runs"]
            ])
        if "fixed-result.json" in bundle:
            doc.heading("同一开发题在正常路由和固定注入下的结果")
            doc.paragraph("每个格子是独立采样的配对。正常路由参与门禁；固定注入只用于诊断技能正文。"
                          "两种路由策略之间没有共享随机种子。")
            doc.table(["路由", "任务", "重复", "父版实际路由", "父版评分", "子版实际路由", "子版评分"], [
                [mode, NAMES[v["task_id"]], v["repeat"] + 1, v["baseline_route"], "通过" if v["baseline_pass"] else "失败",
                 v["candidate_route"], "通过" if v["candidate_pass"] else "失败"]
                for mode, rows in (("正常", r["rows"]), ("固定注入", bundle["fixed-result.json"]["rows"])) for v in rows
            ])
        if "check-repair-result.json" in bundle:
            retry = bundle["check-repair-result.json"]
            doc.heading("定向补测能否修好模型自己的断言")
            doc.paragraph("这项追加诊断复用已运行的学习补丁。每条轨迹只取最后一次真实触发 AssertionError 的自写检查。"
                          "模型看到原脚本、自己的报错和公开要求，获得至多三个专用于补测的动作。控制方未提供关闭用例的测试代码或预期字符串。"
                          "补测单列用量和结果，正式修复及留出门禁继续沿用主实验的评分。")
            doc.table(["来源", "既有学习题", "调用检查次数", "在原补丁返回 0", "检出关闭回归"], [
                ["父版轨迹" if t["role"] == "parent" else "子版轨迹", t["task"], len(t["attempts"]),
                 sum(a["exit"] == 0 for a in t["attempts"]), "是" if any(a["caught_disabled"] for a in t["attempts"]) else "否"]
                for t in retry["trials"]
            ])
        if "coverage-feedback-result.json" in bundle:
            doc.heading("把脚本实际跑过的开关值反馈给模型")
            doc.paragraph("两份修到全绿的 Bash 脚本都重复了公开用例的开启状态。控制器从真实 Bash 补全调用中读取了 "
                          "case_sensitive 的值，并告知模型尚缺 False。每份补丁只有一次自写脚本的机会。"
                          "测试代码及断言中的预期字符串均由 GLM 编写。以下行来自同一道既有学习题上的两条轨迹。")
            doc.table(["来源", "原补丁退出码", "上游修复退出码", "关闭变体退出码", "断言失败行", "检出关闭回归"], [
                ["父版轨迹" if a["role"] == "parent" else "子版轨迹", a.get("exit"), a.get("reference", {}).get("exit"),
                 a.get("off_mutant", {}).get("exit"), a.get("off_mutant", {}).get("line"),
                 "是" if a["caught_disabled"] else "否"] for a in bundle["coverage-feedback-result.json"]["rows"]
            ])
    claim = bundle.get("step-claim-audit.json")
    if claim and not claim["accepted_as_historical_fact"]:
        doc.heading(f"子版第 {claim['step_index_zero_based'] + 1} 步的一条历史陈述缺少证据")
        doc.paragraph(
            f"GLM 在步骤正文写道 {claim['historical_assertion']}。已核对它引用的 {len(claim['cited_runs'])} 条学习运行，"
            f"与陈述匹配的同次公开执行有 {len(claim['same_invocation_matches'])} 条，其中满足时间顺序的有 {len(claim['exact_matches'])} 条。"
            "该句不能作为已经观察到的事实。它位于操作步骤正文，已验证的摘要回执没有为它提供支持。本次核查未改变实验门禁或原始技能。"
        )
        doc.table(["原始语句、逐次公开测试和补丁哈希"], [[("核查原始证据", "step-claim-audit.json")]])
    doc.heading("故障学习来源与程序步骤")
    independent = bundle["independence.json"]
    doc.table(
        ["学习任务", "上游修复提交", "涉及的函数"],
        [
            [
                case["description"],
                (
                    case["fixed_commit"][:12],
                    "https://github.com/pallets/click/commit/" + case["fixed_commit"],
                ),
                ", ".join(case["functions"]),
            ]
            for case in independent["tasks"]
        ],
    )
    doc.paragraph(
        "独立性核查覆盖任务、修复提交、补丁和缺陷分组。本组学习案例均来自 Click，部分源码文件可能重叠。"
    )

    def trace(key):
        return key[:12], f"traces/{key}.json"

    def result(x):
        return "通过" if x else "失败"

    def state(x):
        return result(x["passed"]) if x["state"] in {"COMPLETE", "FAILED"} else x["state"]

    doc.table(
        ["独立学习任务", "重复", "父版公开轨迹", "最终结果", "送入提案的失败证据", "子版引用"],
        [
            [
                x["task_id"],
                x["repeat"] + 1,
                trace(x["run_id"]),
                state(x),
                refs.get(x["run_id"], "—"),
                "是" if x["run_id"] in cited else "—",
            ]
            for x in r["learning"]
        ],
    )
    if log["step_edits"]:
        doc.paragraph(
            "下表对照两个冻结版本，逐段展示 GLM 实际写入的步骤。两个版本的完整技能、程序生成的摘要和引用回执见本页证据文件。"
        )

        def lines(start, steps, candidate=False):
            rendered = []
            for i, step in enumerate(steps, start + 1):
                flag = (
                    candidate
                    and claim
                    and not claim["accepted_as_historical_fact"]
                    and i == claim["step_index_zero_based"] + 1
                )
                note = "〔此步括号里的历史观察未获引用轨迹支持〕" if flag else ""
                rendered.append(f"{i}. {step}{note}")
            return " ".join(rendered) or "—"

        doc.table(
            ["父版原步骤", "子版新步骤"],
            [
                [lines(e["before_index"], e["before"]), lines(e["after_index"], e["after"], candidate=True)]
                for e in log["step_edits"]
            ],
        )
    doc.heading("开关验证与可回退补丁")
    doc.paragraph(
        "开启和关闭均以求解器主动请求的冻结公开测试为证，两个命名节点须出现在同一份实测输出中。"
        "“两态检查／均通过”统计检查次数。原开发题未注册这类命名节点时标为未登记。"
        "存档列按通过公开测试且不同于初始源码的补丁哈希去重。实质回退要求 GLM 调用恢复且前后源码哈希不同。"
    )

    def behavior(key):
        b = summaries[key]
        states = f"{b['paired_checks']} / {b['both_passed']}" if b["named"] else "未登记"
        per_state = b["per_state"]
        return [
            states,
            *[
                f"{per_state[k]['passed']} / {per_state[k]['failed']}" if k in b["registered"] else "未登记"
                for k in ("enabled", "disabled")
            ],
            b["changed_green_hashes"],
            f"{b['changed_restores']} / {b['restores']}",
            (key[:12], f"behavior/{key}.json"),
        ]

    matrix = []
    for label, trials in (("父版学习", r["learning"]), ("子版学习回放", r["learning_replay"])):
        for t in trials:
            matrix.append([label, t["task_id"], t.get("repeat", 0) + 1, state(t), *behavior(t["run_id"])])
    for t in r["rows"]:
        for arm, name in (("baseline", "开发父版"), ("candidate", "开发子版")):
            matrix.append(
                [
                    name,
                    NAMES[t["task_id"]],
                    t["repeat"] + 1,
                    result(t[arm + "_pass"]),
                    *behavior(t[arm + "_run"]),
                ]
            )
    doc.table(
        [
            "阶段",
            "任务",
            "重复",
            "独立结果",
            "两态检查／均通过",
            "开启通过／失败",
            "关闭通过／失败",
            "改动后绿点",
            "实质回退／成功恢复",
            "行为回执",
        ],
        matrix,
    )
    changed = sum(b["changed_restores"] for b in summaries.values())
    doc.paragraph(
        f"所有已核验运行中，模型发起且确实改变工作区的成功恢复共 {changed} 次。"
        "Docker 回归测试另用预设动作验证绿点保存、改坏、恢复旧版本及重新评分。该工程测试不计入 GLM 恢复次数或技能收益。"
    )
    if "learning-patch-probe.json" in bundle:
        probe = bundle["learning-patch-probe.json"]
        doc.heading("子版学习回放中的公开通过与独立失败")
        doc.paragraph(
            f"运行 {probe['run_id'][:12]} 在提交时通过冻结公开测试，独立评分失败。事后用原始版本、子版提交补丁和上游修复分别运行相同输入，"
            f"{probe['matched_case_count']} 个输入与上游一致，{probe['mismatched_case_count']} 个出现差异。下列文本用 JSON 转义显示终端控制字符。"
        )
        rows = []
        for name in probe["mismatched_cases"]:
            c = probe["cases"][name]
            rows.append(
                [
                    name,
                    json.dumps(c["candidate"]["stripped"]),
                    json.dumps(c["reference"]["stripped"]),
                    "一致" if c["candidate"]["color_on_help"] == c["reference"]["color_on_help"] else "差异",
                    "一致"
                    if c["candidate"]["color_off_help"] == c["reference"]["color_off_help"]
                    else "差异",
                ]
            )
        doc.table(
            ["复验输入", "子版移除颜色后的文本", "上游文本", "开启颜色的帮助输出", "关闭颜色的帮助输出"], rows
        )
        doc.paragraph(
            "这些输入是查看补丁后选择的公开行为检查。它们复现了可观察的边界错误，正式评分与门禁沿用原记录。"
        )
        doc.table(["源码、三方输出和原始评分证据"], [[("学习补丁复验明细", "learning-patch-probe.json")]])
    if "checkpoint-replay.json" in bundle:
        replay = bundle["checkpoint-replay.json"]
        if replay["status"] == "verified":
            doc.paragraph(
                f"控制器额外选取 GLM 真实学习运行 {replay['source']['run_id'][:12]} 的绿点，重新使用冻结公开命令验证，"
                "换入已知缺陷版本，再显式恢复原补丁。该过程由控制器执行，模型调用数为零。"
            )
            doc.table(
                ["离线工程复演", "补丁", "公开退出码", "开启", "关闭"],
                [
                    [
                        label,
                        x["workspace_hash"][:12],
                        x["exit_code"],
                        x["named_states"]["enabled"],
                        x["named_states"]["disabled"],
                    ]
                    for label, x in zip(("真实绿点", "原始缺陷", "恢复工作区"), replay["stages"], strict=True)
                ],
            )
    doc.heading("父子对照与新题门禁")

    def paired(t):
        cells = [NAMES.get(t["task_id"], t["task_id"]), t["repeat"] + 1]
        for arm in ("baseline", "candidate"):
            key = t[arm + "_run"]
            cells.append((t[arm + "_route"] + " · " + result(t[arm + "_pass"]), f"traces/{key}.json"))
        return cells

    doc.table(["焦点题", "重复", "父版正常路由", "子版正常路由"], [paired(t) for t in r["rows"]])
    gate = r.get("focus_gate")
    if gate:
        doc.paragraph(
            f"两个焦点开发分组中，连续两次出现与技能行为相关改善的分组数为 {len(gate['repeat_confirmed_improved_groups'])}，"
            f"出现退化的题目为 {'、'.join(NAMES.get(k, k) for k in gate['regressions']) or '无'}。"
        )
    full = r["gate"]
    hold = r["holdout"]
    doc.paragraph(
        "正式门禁要求至少两个独立开发分组各重复改善两次，且开发、支持和历史回归零观察退化。"
        f"完整门禁状态为{STATUS.get(full['status'], full['status'])}，新题状态为{STATUS.get(hold['status'], hold['status'])}。"
    )
    if full["rows"][len(r["rows"]) :]:
        doc.table(
            ["其余开发／支持／历史回放", "重复", "父版", "子版"],
            [paired(t) for t in full["rows"][len(r["rows"]) :]],
        )
    if hold["rows"]:
        doc.table(["留出任务", "重复", "父版", "子版"], [paired(t) for t in hold["rows"]])
    if "patch-probes.json" in bundle:
        prob = bundle["patch-probes.json"]
        rows = []
        for trial in prob["trials"]:
            if not trial["paired"] or not trial["patch_hash"]:
                continue
            task = trial["task_id"]
            names = (
                ("default_help", "disabled_help")
                if task.endswith("42e2821ba3")
                else ("implicit_count", "explicit_numeric_bounds")
            )
            observed = prob["observations"][task][trial["patch_hash"]].get("cases", {})
            cells = [NAMES[task], trial["repeat"], "父版" if trial["arm"] == "baseline" else "子版"]
            for key in names:
                cells.append(
                    ("与参考一致" if observed[key] == prob["reference_cases"][task][key] else "差异")
                    if key in observed
                    else "无法测量"
                )
            rows.append(cells)
        doc.heading("提交后独立探针")
        doc.paragraph(
            "此处逐份补丁重放用户可见输出。#557 的两列为默认帮助与关闭帮助，计数题为计数标志与显式数值范围。求解器在运行时是否检查过这两种情况，须查阅前文的行为回执。"
        )
        doc.table(["任务", "重复", "版本", "默认帮助／计数标志", "关闭帮助／显式数值范围"], rows)
    if not r["completed"]:
        doc.paragraph("实验中断。现有收据只覆盖已登记运行，部分配对、门禁或新题结果尚未形成。")
    doc.heading("用量与可核对文件")
    c = r["costs"]
    doc.paragraph(
        f"本次共登记 {c['calls']} 个模型请求。已结算估算 {c['cost_credits']:.5f} 套餐积分，"
        f"仍有 {c['reserved_credits']:.5f} 积分预留。提案和有账单的中断尝试均计入。"
    )
    names = {
        "protocol.json": "冻结协议",
        "result.json": "实验终态",
        "revision.json": "父子技能与来源",
        "step-program.json": "GLM 生成的条件、步骤和历史观察引用",
        "fixed-plan.json": "固定注入对照的预登记",
        "fixed-result.json": "固定注入父子对照与用量",
        "check-repair-plan.json": "定向修复已有学习断言的输入与预算",
        "check-repair-result.json": "定向补测脚本、执行、变体重放和用量",
        "coverage-feedback-plan.json": "公开开关覆盖差异与单次补测登记",
        "coverage-feedback-result.json": "补充实测开关反馈后的两次真实 GLM 调用",
        "fixed-traces.json": "固定注入的逐次执行轨迹",
        "step-admission.json": "第 7 步的准入回归",
        "cli-controls.json": "开启用例与关闭变体校准",
        "cli-reach.json": "当时的补丁是否真正走到相同的关闭调用",
        "learn-postmortem.json": "子版冻结后对已提交学习补丁的分支核查",
        "reach-plan.json": "焦点对照前登记的调用到达条件",
        "authored-checks.json": "模型自写 CLI 检查的执行与变体重放",
        "slots.json": "运行顺序及尝试",
        "learning-receipts.json": "公开学习证据目录",
        "summary-receipt.json": "摘要绑定",
        "independence.json": "学习任务的上游提交与独立性核查",
        "checkpoint-replay.json": "真实绿点的独立工程恢复复演",
        "step-claim-audit.json": "步骤正文历史陈述逐次运行核对",
        "learning-patch-probe.json": "已提交学习补丁的公开行为复验",
        "learn-verification.json": "学习缺陷原版及上游修复版验证",
        "audit.json": "门禁、行为与用量核对",
        "patch-probes.json": "已提交焦点补丁的事后探针（独立于门禁）",
    }
    doc.table(["公开证据"], [[(label, key)] for key, label in names.items() if key in bundle])
    doc.write(output)


def export(root, output):
    result, bundle = audit(root.resolve())
    for name, data in bundle.items():
        path = output / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    render(output, result, bundle)
    return bundle["audit.json"]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    summary = export(args.root, args.output)
    print(json.dumps({key: summary[key] for key in ("trial_count", "paired_runs", "model_calls_for_report")}))
