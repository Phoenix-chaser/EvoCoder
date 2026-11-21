import importlib
import json
from pathlib import Path

import pytest

from evocoder.contracts import digest, model_task_view
from evocoder.gate import gate_summary


@pytest.fixture
def study(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).parents[1] / "scripts"))
    return importlib.import_module("run_object_revision")


def test_new_object_tasks_are_independent_and_hide_grader(study):
    proto = json.loads(study.PUBLIC.read_text())
    cases = study.cases()
    assert [t.id for t, _ in cases] == proto["learn"]
    old = {r["source_group"] for r in proto["tasks"] if r["id"] not in proto["learn"]}
    new = {t.source_group for t, _ in cases}
    assert new.isdisjoint(old) and len(new) == 6
    for (task, ref), cal in zip(cases, proto["calibration"], strict=True):
        assert task.split == "learn" and cal["reference"] == digest(ref)
        assert digest(task.model_dump()) == cal["snapshot"]
        assert cal["broken"]["controls"] and not cal["broken"]["public"] and all(cal["fixed"].values())
        assert not set(task.grader_files) & set(model_task_view(task.model_dump())["files"])


def test_success_and_routing_skip_cannot_trigger_revision(study):
    f = {
        "run": "applied-failure",
        "passed": False,
        "patch": "p",
        "eligible": True,
        "applied": study.LEARNED,
        "route": "forced",
        "feedback": {"learn_submission_failed_independent_check": True},
    }
    skip = dict(f, run="not-applied", applied=None, eligible=False, route="skip")
    green = dict(f, run="applied-success", eligible=False, passed=True)

    def row(t, a):
        return {"task": t, "repeat": 0, "order": ["parent"], "parent": a}

    assert study.select_evidence([row("x", skip), row("y", green)]) == []
    assert study.select_evidence([row("x", skip), row("x", f), row("y", green)]) == [
        "applied-failure",
        "not-applied",
        "applied-success",
    ]


def test_object_revisions_require_concurrent_normal_and_deployed_gates(study):
    def pair(t, n):
        return {
            "task_id": t,
            "source_group": t,
            "split": "dev",
            "repeat": n,
            "candidate_pass": True,
            "baseline_pass": False,
            "skill_behavior": "candidate_applied",
            "infrastructure_error": False,
        }

    rows = [pair(t, n) for n in range(2) for t in ["a", "b"]]
    passing = gate_summary(rows, 2)
    assert study.release_decision(rows, passing, passing)["passed"]
    assert not study.release_decision(rows, passing, {"gate": {"passed": False}})["passed"]
    skipped = [dict(r, skill_behavior="neither_applied") for r in rows]
    assert not study.release_decision(skipped, passing, passing)["passed"]
    regress = dict(
        rows[0], task_id="original-support", split="learn", baseline_pass=True, candidate_pass=False
    )
    assert not study.release_decision(rows, gate_summary(rows + [regress], 2), passing)["passed"]


def test_matched_public_check_requires_same_task_and_forced_parent(study):
    from object_witness import matched_learning

    fail = {
        "run": "bad",
        "eligible": True,
        "route": "forced",
        "passed": False,
        "patch": "p1",
        "applied": study.LEARNED,
        "feedback": {"learn_submission_failed_independent_check": True},
    }
    success = dict(fail, run="good", eligible=False, passed=True, patch="p2")

    def row(t, n, a):
        return {"task": t, "repeat": n, "order": ["fixed_parent"], "fixed_parent": a}

    with pytest.raises(ValueError, match="no matched"):
        matched_learning([row("a", 0, fail), row("b", 1, success)])
    with pytest.raises(ValueError, match="no matched"):
        matched_learning([row("a", 0, fail), row("a", 1, dict(success, route="skip", applied=None))])
    assert matched_learning([row("a", 0, fail), row("a", 1, success)])["refs"] == ["bad", "good"]


def test_public_witness_uses_runtime_behavior(study):
    from object_witness import validate_program

    task, _ = study.cases()[0]
    validate_program("from tiles import Compositor\nassert callable(Compositor)", task)
    for source in (
        "import inspect\nassert 1",
        "from tiles import Compositor\nassert open('tiles.py')",
        "from tiles import Compositor\nassert Compositor.__dict__",
        "assert True",
    ):
        with pytest.raises(ValueError):
            validate_program(source, task)


def test_witness_author_receives_blinded_submissions_and_public_inputs(study):
    from object_witness import draft

    task, reference = study.cases()[0]
    broken = {n: task.files[n] for n in task.editable}

    class Capturing:
        def request(self, schema, system, data, run_id):
            assert set(data) == {
                "issue",
                "contract",
                "public_example",
                "public_modules",
                "submission_A",
                "submission_B",
            }
            assert {digest(data["submission_A"]), digest(data["submission_B"])} == {
                digest(broken),
                digest(reference),
            }
            for name in task.grader_files:
                assert name not in json.dumps(data)
            return schema(
                tool="write",
                path="behavior_check.py",
                content="from tiles import Compositor\nassert callable(Compositor)",
            )

    assert "assert" in draft(Capturing(), "learn-check", task, [reference, broken])


@pytest.mark.docker
def test_witness_distinguishes_assertion_failure_from_runtime_error(study):
    import subprocess
    from evocoder.contracts import Settings
    from object_witness import verdict

    task, fixed = next((t, p) for t, p in study.cases() if t.id == "learn-undo-journal")
    image = subprocess.check_output(
        ["docker", "image", "inspect", "--format", "{{.Id}}", "python:3.12-slim"], text=True
    ).strip()
    cfg = Settings(_env_file=None, mode="mock", image=image)
    check = "from document import begin\nj=[]\nbegin(j).record('a')\nassert j == ['a']"
    assert verdict(task, {}, check, cfg) == "assertion_failed"
    assert verdict(task, fixed, check, cfg) == "passed"
    assert (
        verdict(task, fixed, "from document import begin\nassert begin().unknown()", cfg) == "execution_error"
    )
