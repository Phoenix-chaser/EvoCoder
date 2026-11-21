import json
import sys

import pytest

from evocoder.dataset import fixture_task
from evocoder.evolution import Evolution
from evocoder.experiment import demo
from evocoder.runtime import Runner, grade, verify_public
from evocoder.sandbox import Sandbox
from evocoder.store import Store

pytestmark = pytest.mark.docker


def test_real_feedback_revision_gate_release_and_future_usage(settings, tmp_path):
    report = demo(settings, tmp_path / "demo")
    assert report["evaluation"]["gate"]["passed"]
    assert report["evaluation"]["baseline_correct"] == 2
    assert report["evaluation"]["candidate_correct"] == 4
    assert report["evaluation"]["regressions"] == []
    assert report["active"] == report["v2"]
    assert all(r["skill_id"] == report["v2"] and r["result"]["passed"] for r in report["test_runs"])
    assert report["budget"]["spent_usd"] == 0
    assert report["claim"] == "engineering_validation_only"
    assert "[redacted]" not in (tmp_path / "demo/report.html").read_text()
    with Store(tmp_path / "demo/state").connect() as db:
        record = json.loads(db.execute("SELECT payload FROM events WHERE kind='SUMMARY_VERIFIED'").fetchone()[0])
    assert record["candidate"] == report["v2"]


def test_mock_cli_proposal_uses_snapshots_from_its_learning_runs(tmp_path, monkeypatch, capsys):
    from evocoder.cli import main
    from evocoder.dataset import create_fixtures

    bundle, state = tmp_path / "tasks", tmp_path / "state"
    create_fixtures(bundle)
    env = tmp_path / "offline.env"
    env.write_text("EVOCODER_MODE=mock\nEVOCODER_API_KEY=\n")

    def invoke(args, expected_exit=0):
        monkeypatch.setattr(sys, "argv", ["evocoder", "--env-file", str(env), "--state", str(state), *args])
        code = main()
        captured = capsys.readouterr()
        assert code == expected_exit, captured.err
        return json.loads(captured.out)

    first = invoke(["run", str(bundle / "learn-01.json"), "--no-skill"])
    root = invoke(["propose", "--runs", first["id"]])["skill_id"]
    trial = invoke(["run", str(bundle / "learn-02.json"), "--skill", root, "--candidate"], 1)
    revised = invoke(["propose", "--parent", root, "--runs", first["id"], trial["id"]])
    assert trial["id"] in revised["skill"]["change_summary"]
    with Store(state).connect() as db:
        record = json.loads(db.execute("SELECT payload FROM events WHERE kind='SUMMARY_VERIFIED'").fetchone()[0])
    assert record["candidate"] == revised["skill_id"]


def test_same_quality_candidate_rejected_and_baseline_retained(settings, store):
    runner, evo = Runner(settings, store), Evolution(settings, store)
    initial = runner.solve(fixture_task("learn-initial", "learn", False))
    v1 = evo.propose([initial])
    tasks = [fixture_task("dev-a", "dev", True), fixture_task("dev-b", "dev", False, 1)]
    result = evo.evaluate(v1, tasks)
    assert not result["gate"]["passed"]
    assert store.active() is None
    with pytest.raises(ValueError, match="passing receipt"):
        store.activate(v1, result["receipt"], None)


def test_solver_never_has_hidden_tests_and_source_only_export(settings):
    task = fixture_task("learn-boundary", "learn", True)
    with Sandbox(task.files, settings, "isolation-test") as box:
        seen = box.execute(["python", "-c", "import pathlib; print(pathlib.Path('judge_contract.py').exists())"])
        assert seen["output"].strip() == "False"
        # Replacing the public test to pass does not modify the independent grading snapshot.
        box.execute(["python", "-c", "open('public_tests.py','w').write('print(\"fake pass\")')"])
        assert box.execute(task.public_command)["exit_code"] == 0
        source = box.export(task.editable)
    assert not grade(task, source, settings, "fresh-grader-test")["passed"]


def test_container_limits_and_no_host_mounts(settings):
    from evocoder.sandbox import bounded_process
    with Sandbox({"a.py": "x=1"}, settings, "resource-test") as box:
        metadata = bounded_process(["docker", "inspect", box.container], 5)
        container = json.loads(metadata["output"])[0]
        assert container["HostConfig"]["NetworkMode"] == "none"
        assert container["HostConfig"]["ReadonlyRootfs"]
        assert container["HostConfig"]["PidsLimit"] == 64
        assert container["Config"]["User"] == "65534:65534"
        assert not any(m["Type"] == "bind" for m in container["Mounts"])
        assert box.execute(["sh", "-c", "echo no > /etc/evocoder-probe"])["exit_code"] != 0


def test_symlink_export_rejected(settings):
    with Sandbox({"a.py": "x=1"}, settings, "symlink-test") as box:
        box.execute(["sh", "-c", "rm a.py && ln -s /etc/passwd a.py"])
        with pytest.raises(ValueError, match="source export"):
            box.export(["a.py"])


def test_command_timeout_destroys_container(settings):
    with Sandbox({}, settings.model_copy(update={"command_timeout": 1}), "timeout-test") as box:
        with pytest.raises(RuntimeError, match="sandbox destroyed"):
            box.execute(["python", "-c", "import time; time.sleep(20)"])
        assert box.container is None


def test_frozen_public_verification_detects_skipped_or_modified_tests(settings, store):
    from evocoder.contracts import Action
    task = fixture_task("public-verification", "learn", True)
    class SkippingModel:
        def request(self, *args):
            return Action(tool="finish", reason="Claim done without tests")
    run_id = Runner(settings, store, SkippingModel()).solve(task)
    run = store.run(run_id)
    assert run["state"] == "FAILED"
    assert run["result"]["public_failed"]
    evidence = Evolution(settings, store).freeze([run_id])[1]
    projected = Evolution.project(evidence)
    observations = projected["episodes"][0]["observations"]
    assert any(o["exit_code"] != 0 for o in observations)
    assert "GRADE" not in str(evidence)


def test_validation_deadlock_is_failed_public_feedback_not_infrastructure_error(settings):
    task = fixture_task("deadlock-validation", "learn", True)
    task = task.model_copy(update={"public_command": ["python", "-c", "import time; time.sleep(20)"]})
    result = verify_public(task, {p: task.files[p] for p in task.editable},
                           settings.model_copy(update={"command_timeout": 1}), "validation-timeout")
    assert result["exit_code"] == 124
    assert result["timeout"]


def test_step_exhaustion_retains_actual_image_identity(settings, store):
    from evocoder.contracts import Action
    task = fixture_task("exhausted-image", "learn", False)
    class RepeatingModel:
        def request(self, *args):
            return Action(tool="read", path=task.editable[0], reason="Inspect repeatedly")
    run_id = Runner(settings.model_copy(update={"max_steps": 4}), store, RepeatingModel()).solve(task)
    run = store.run(run_id)
    assert run["state"] == "EXHAUSTED"
    assert run["result"]["image_id"].startswith("sha256:")


def test_large_source_windows_and_exact_edit_are_atomic(settings):
    text = "x = 0\n" * 20000 + "needle = 1\n"
    with Sandbox({"large.py": text, "other.py": "needle = 0\n"}, settings, "large-source-edit") as box:
        matches = box.search("needle", "large.py")
        assert "large.py:20001:needle = 1" in matches["output"]
        assert "other.py" not in matches["output"]
        window = box.read("large.py", 20000, 2)
        assert "20001: needle = 1" in window["output"]
        assert box.edit("large.py", "x = 0", "x = 2")["exit_code"] != 0
        assert box.export(["large.py"])["large.py"] == text
        assert box.edit("large.py", "needle = 1", "needle = 2")["exit_code"] == 0
        assert box.export(["large.py"])["large.py"] == text.replace("needle = 1", "needle = 2")
