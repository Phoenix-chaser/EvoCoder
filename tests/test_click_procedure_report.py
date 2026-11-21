import importlib.util
import sqlite3
from copy import deepcopy
from pathlib import Path

import pytest

from evocoder.gate import gate_summary


def script():
    path = Path(__file__).resolve().parents[1] / "scripts/report_click_procedure.py"
    spec = importlib.util.spec_from_file_location("procedure_reporting", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_learning_evidence_requires_both_parent_application_and_actual_failure():
    mod = script()
    params = {"parent": "A", "learn_ids": ["l"]}
    failure = {
        "run_id": "a",
        "task_id": "l",
        "applied_skill": "A",
        "route": "apply",
        "eligible_for_revision": True,
        "state": "EXHAUSTED",
        "passed": False,
        "repeat": 0,
        "execution_feedback": {"step_budget_exhausted": True},
    }
    assert mod.check_evidence([failure], ["a"], params) == {"a": "终态耗尽"}
    transient = failure | {
        "state": "COMPLETE",
        "passed": True,
        "execution_feedback": {"post_edit_public_failure_sequences": [11]},
    }
    with pytest.raises(ValueError, match="premature"):
        mod.check_evidence([transient], ["a"], params)
    second = transient | {"run_id": "b", "repeat": 1}
    assert len(mod.check_evidence([transient, second], ["a", "b"], params)) == 2
    for invalid in (
        {"task_id": "dev"},
        {"applied_skill": "B"},
        {"route": "skip"},
        {"eligible_for_revision": False},
    ):
        with pytest.raises(ValueError):
            mod.check_evidence([failure | invalid], ["a"], params)


@pytest.fixture
def focus():
    mod = script()
    a, b = list(mod.NAMES)
    p = {"dev": [a, b, "other"], "focus": [a, b], "holdouts": ["hold"], "sentinels": ["s"]}
    rows = [
        {
            "task_id": key,
            "repeat": i,
            "source_group": key,
            "split": "dev",
            "baseline_pass": True,
            "candidate_pass": False,
            "skill_behavior": "candidate_applied",
            "infrastructure_error": False,
        }
        for i in range(2)
        for key in (a, b)
    ]
    r = {
        "rows": rows,
        "gate": {"rows": [], "status": "blocked_by_focus"},
        "completed": True,
        "candidate": "B",
        "holdout": {"rows": [], "status": "blocked_by_focus"},
        "focus_gate": gate_summary(rows, 2),
    }
    return mod, p, r


@pytest.mark.parametrize("tamper", ["duplicate", "omit", "touch_holdout", "claim_win", "start_full"])
def test_recomputed_focus_gate_blocks_premature_holdout(focus, tamper):
    mod, p, r = focus
    mod.check_schedule(p, r, [])
    changed = deepcopy(r)
    if tamper == "duplicate":
        changed["rows"][2] = changed["rows"][0]
    elif tamper == "omit":
        changed["rows"].pop()
    elif tamper == "touch_holdout":
        changed["holdout"]["rows"] = [{"task_id": "hold", "repeat": 0}]
    elif tamper == "start_full":
        changed["gate"]["rows"] = r["rows"]
    else:
        changed["focus_gate"]["gate"]["passed"] = True
    with pytest.raises(ValueError):
        mod.check_schedule(p, changed, [])


def test_states_and_restores_do_not_count_skips_or_unchanged_workspace():
    data = {
        "public_nodes": {"enabled": "on", "disabled": "off"},
        "solver_requested_public_tests": [
            {"named_states": {"enabled": "passed"}},
            {"named_states": {"enabled": "passed", "disabled": "failed"}},
        ],
        "saved_green": [{"patch_hash": "h", "changed_from_input": True}] * 2,
        "restored_green": [
            {"changed_workspace": True},
            {"changed_workspace": False},
            {"changed_workspace": None},
        ],
        "failed_restores": [],
    }
    m = script().features(data)
    assert (m["paired_checks"], m["both_passed"], m["green_saves"], m["changed_green_hashes"]) == (1, 0, 2, 1)
    assert (m["restores"], m["changed_restores"], m["unknown_before_restores"]) == (3, 1, 1)


def test_offline_cost_reads_only_prefix_of_shared_ledger_and_includes_unsettled(tmp_path):
    mod = script()
    path = tmp_path / "ledger.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute("create table calls(run_id,state,actual,reserved)")
        db.executemany(
            "insert into calls values(?,?,?,?)",
            [
                ("ancient", "settled", 9, 13),
                ("a", "settled", 10, 15),
                ("b", "reserved", None, 4),
                ("future", "settled", 22, 30),
            ],
        )
    p = {"previous_billing_row": 1, "budget_before": {"spent_credits": 0}}
    r = {
        "billed_ids": ["a", "b"],
        "costs": {"calls": 2, "cost_credits": 1e-8, "reserved_credits": 4e-9},
        "budget_after": {"spent_credits": 1e-8},
    }
    got = mod.billing(mod.Archive(path), p, r)
    assert set(got) == {"a", "b"} and got["b"]["reserved_nanos"] == 4
    for invalid in (r | {"billed_ids": ["b"]}, r | {"costs": r["costs"] | {"reserved_credits": 0}}):
        with pytest.raises(ValueError):
            mod.billing(mod.Archive(path), p, invalid)


def test_trace_export_retains_public_evidence_only_and_escapes_report_html(tmp_path):
    mod = script()
    ev = [
        {"seq": 1, "kind": "MODEL_RESPONSE", "payload": {"provider-private": "x"}},
        {"seq": 2, "kind": "GRADE", "payload": {"passed": True, "hidden-log": "x"}},
        {"seq": 3, "kind": "PUBLIC_VERIFY", "payload": {"observation": {"output": "public"}}},
    ]
    assert mod.public_events(ev) == [{"seq": 2, "kind": "GRADE", "payload": {"passed": True}}, ev[2]]
    d = mod.Report()
    d.heading("<unsafe>")
    d.table(["Step"], [[("<w>", "traces/abc.json")]])
    d.write(tmp_path)
    source = (tmp_path / "report.html").read_text()
    assert "<unsafe>" not in source and "&lt;w&gt;" in source and 'href="traces/abc.json"' in source


def test_full_gate_requires_complete_support_and_each_holdout_repeat(focus):
    mod, p, r = focus
    for row in r["rows"]:
        row.update(baseline_pass=False, candidate_pass=True)
    r["focus_gate"] = gate_summary(r["rows"], 2)
    rows = r["rows"].copy()
    for role, key, count, split in (
        ("other_dev", "other", 2, "dev"),
        ("known_regression", "s", 2, "dev"),
        ("support", "learn", 1, "learn"),
    ):
        for i in range(count):
            rows.append(
                {
                    "evaluation_role": role,
                    "task_id": key,
                    "repeat": i,
                    "source_group": key,
                    "split": split,
                    "baseline_pass": True,
                    "candidate_pass": True,
                    "skill_behavior": "candidate_applied",
                    "infrastructure_error": False,
                }
            )
    r["gate"] = {"status": "complete", "rows": rows, **gate_summary(rows, 2)}
    r["holdout"] = {"status": "complete", "rows": [{"task_id": "hold", "repeat": i} for i in range(2)]}
    mod.check_schedule(p, r, {"learn"})
    for field in ("gate", "holdout"):
        bad = deepcopy(r)
        bad[field]["rows"].pop()
        with pytest.raises(ValueError):
            mod.check_schedule(p, bad, {"learn"})


def learning_probe_case():
    from evocoder.contracts import Task, digest

    task = Task(
        id="learn",
        repository="fixture",
        source_group="unique",
        split="learn",
        issue="format",
        files={"f.py": "old"},
        editable=["f.py"],
        public_command=["check"],
        grader_files={},
        grader_command=["grade"],
    )
    protocol = {"learn_ids": ["learn"]}
    ref = {"f.py": "reference"}
    candidate = digest({"f.py": "candidate"})
    run = {
        "id": "r",
        "purpose": "evaluate",
        "task_id": task.id,
        "task_hash": digest(task.model_dump()),
        "result": {"patch": candidate, "passed": False},
    }
    events = [
        {"kind": "PATCH", "seq": 1, "payload": {"artifact": candidate}},
        {"kind": "PUBLIC_VERIFY", "seq": 2, "payload": {"observation": {"exit_code": 0}}},
        {"kind": "GRADE", "seq": 3, "payload": {"passed": False}},
    ]
    one = {
        "stripped": "x",
        "visible_length": 1,
        "color_off_help": "x",
        "color_on_help": "x",
        "exit_codes": [0, 0],
    }
    probe = {
        "schema": 1,
        "model_calls": 0,
        "post_hoc": True,
        "used_for_gate": False,
        "study_protocol_hash": digest(protocol),
        "run_id": "r",
        "task_id": "learn",
        "task_hash": run["task_hash"],
        "split": "learn",
        "run_result_hash": digest(run["result"]),
        "patch_hash": candidate,
        "reference_hash": digest(ref),
        "source_hashes": {"original": digest(task.files), "candidate": candidate, "reference": digest(ref)},
        "evidence_refs": {"PATCH": 1, "PUBLIC_VERIFY": 2, "GRADE": 3},
        "recorded_public_exit": 0,
        "recorded_grade": False,
        "cases": {
            "case": {
                "original": one,
                "reference": one,
                "candidate": one | {"color_off_help": "xx"},
                "matches_reference": False,
                "differing_fields": ["color_off_help"],
            }
        },
        "matched_case_count": 0,
        "mismatched_case_count": 1,
        "mismatched_cases": ["case"],
    }
    return probe, (protocol, run, events, task, ref)


@pytest.mark.parametrize("invalid", ["run", "patch", "scope", "outcomes", "count", "grade"])
def test_learning_patch_diagnostic_matches_archived_sources_and_observed_outputs(invalid):
    mod = script()
    probe, args = learning_probe_case()
    mod.validate_learning_probe(probe, *args)
    if invalid == "run":
        probe["run_id"] = "other"
    elif invalid == "patch":
        probe["reference_hash"] = probe["patch_hash"]
    elif invalid == "scope":
        probe["used_for_gate"] = True
    elif invalid == "outcomes":
        probe["cases"]["case"]["candidate"]["color_off_help"] = "x"
    elif invalid == "count":
        probe["matched_case_count"] = 1
    else:
        probe["recorded_grade"] = True
    with pytest.raises(ValueError):
        mod.validate_learning_probe(probe, *args)
