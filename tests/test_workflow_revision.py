import sys
from pathlib import Path

import pytest

from evocoder.contracts import digest
from evocoder.library import SkillLibrary
from test_library import fixture_task, seed

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
from portfolio_routing import CHILD  # noqa: E402
from workflow_cases import cases  # noqa: E402
from workflow_gate import LABEL, PRINT, SCHEDULER, evaluate, plan  # noqa: E402
from workflow_learning import missed, select  # noqa: E402
from workflow_study import FLAGS, PUBLIC, WAKES, read  # noqa: E402


def test_retaining_root_replaces_only_previous_specialization(settings, store, monkeypatch):
    lib = SkillLibrary(settings, store)
    root = seed(settings, store, fixture_task("root", "learn", False))
    old = seed(settings, store, fixture_task("old", "learn", True), root)
    from evocoder.evolution import ProposalModel

    original = ProposalModel.mock_skill
    with monkeypatch.context() as scoped:

        def revised(data):
            skill = original(data)
            return skill.model_copy(
                update={"steps": [*skill.steps, "Exercise the resumed call on a fresh worker"]}
            )

        scoped.setattr(ProposalModel, "mock_skill", staticmethod(revised))
        child = seed(settings, store, fixture_task("new", "learn", True), old)
    sibling = seed(settings, store, fixture_task("side", "learn", True), root)
    other = seed(settings, store, fixture_task("other", "learn", False))
    assert lib.retain_parent([root, old, other], child) == sorted([root, child, other])
    assert lib.retain_parent([root], child) == sorted([root, child])
    with pytest.raises(ValueError):
        lib.retain_parent([root, sibling], child)
    with pytest.raises(ValueError):
        lib.validate_family_pool([root, old, child])


def test_independent_program_calibration_and_sealed_panel():
    p = read(PUBLIC)
    tasks = cases()
    assert len({t.source_group for t, _, _ in tasks.values()}) == len(tasks)
    for tid, (t, ref, program) in tasks.items():
        m = p["programs"][tid]
        assert m["snapshot"] == digest(t.model_dump()) and digest(ref) == m["reference"]
        assert all(m["fixed"].values()) and not m["broken"]["public"]
        if program:
            assert digest(program) == m["feedback"] and t.split == "learn"
        else:
            assert m["partial"]["public"] and m["partial"]["controls"] and not all(m["partial"].values())
    assert not set(tasks) & set(p["holdout_hashes"]) and len(p["holdout_hashes"]) == 30


def test_probe_green_submissions_and_natural_flag_exposure_only():
    base = dict(
        run="flag",
        intent=dict(task=FLAGS[0], arm="previous"),
        selected=CHILD,
        patch="hash",
        passed=True,
        public_feedback={"passed": True},
    )
    wake = dict(
        base,
        run="wake",
        intent=dict(task=WAKES[0], arm="fixed"),
        passed=False,
        probes={"public": True, "controls": True, "resumed": False},
        public_feedback={"passed": False},
    )
    assert [r["run"] for r in select([base, wake])] == ["flag", "wake"]
    assert select([dict(base, intent=dict(task=FLAGS[0], arm="fixed")), wake]) == []
    assert select([base, dict(wake, public_feedback={"passed": True})]) == []
    assert missed(wake) and not missed(dict(wake, passed=None)) and not missed(dict(wake, patch=None))
    assert not missed(dict(wake, probes={**wake["probes"], "controls": False}))


def hypothetical_rows(p, h):
    rows = []
    for t, n in h["counts"].items():
        for k in range(n):
            row = dict(task=t, repeat=k)
            for a in h["arms"][t]:
                is_target = t in h["targets"]
                wrong = a in ("bare", "parent") and is_target and k < 3
                row[a] = dict(
                    selected=h["child"] if a == "revised" and is_target else None,
                    passed=not wrong,
                    first_solver_request="with-skill" if a == "revised" and is_target else "same",
                    probes=dict(controls=True, wake_replay=True),
                    patch="files",
                )
            rows.append(row)
    return rows


def test_gate_uses_actual_new_child_two_sources_and_seven_round_protection():
    p = read(PUBLIC)
    h = plan(p, "revision", "watermark-bus", {SCHEDULER: "snap"})
    assert h["targets"] == [LABEL, "watermark-bus"]
    assert h["counts"][PRINT] == 7 and h["counts"][SCHEDULER] == 5
    rows = hypothetical_rows(p, h)
    assert evaluate(p, h, rows)["passed"]
    affected = next(r for r in rows if r["task"] == "watermark-bus")
    affected["revised"]["selected"] = CHILD
    assert not evaluate(p, h, rows)["passed"]
    affected["revised"]["selected"] = "revision"
    affected = next(r for r in rows if r["task"] == PRINT)
    affected["revised"]["selected"] = "revision"
    affected["revised"]["first_solver_request"] = "different"
    assert not evaluate(p, h, rows)["direct"][PRINT]["passed"]
    affected["revised"]["selected"] = None
    affected["revised"]["first_solver_request"] = "same"
    affected = next(r for r in rows if r["task"] == SCHEDULER)
    affected["previous"]["passed"] = None
    assert not evaluate(p, h, rows)["passed"]
    assert plan(p, "revision", None, {})["mode"] == "protection" and plan(p, None, None, {})["counts"] == {}


def test_learning_selector_requires_strict_scope_gain_and_complete_nonregression():
    from workflow_candidate_selection import choose

    initial = dict(boolean_misroutes=4, wake_pass=3, total_pass=7, complete=True)
    better = dict(boolean_misroutes=0, wake_pass=3, total_pass=7, complete=True)
    assert choose(initial, better)
    assert not choose(initial, initial)
    assert not choose(initial, dict(better, wake_pass=2))
    assert not choose(initial, dict(better, total_pass=6))
    assert not choose(initial, dict(better, complete=False))
    assert not choose(dict(initial, complete=False), better)


def test_gate_collection_keeps_screening_slots_out_of_the_formal_panel():
    from report_workflow_revision import panel_rows

    p = read(PUBLIC)
    h = plan(p, "revision", "watermark-bus", {})
    with pytest.raises(ValueError, match="partial panel"):
        panel_rows([dict(intent=dict(phase="screen", task="watermark-bus", repeat=0, arm="parent"))], h)
