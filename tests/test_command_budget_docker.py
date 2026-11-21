import pytest

from evocoder.contracts import Action
from evocoder.dataset import fixture_task
from evocoder.evolution import Evolution
from evocoder.runtime import Runner

pytestmark = pytest.mark.docker


@pytest.mark.parametrize("tool", ["command", "test"])
def test_solver_command_deadline_is_a_failed_budget_not_an_infrastructure_error(settings, store, tool):
    task = fixture_task("solver-deadline-" + tool, "learn", False)
    slow = ["python", "-c", "import time; time.sleep(20)"]
    if tool == "test":
        task = task.model_copy(update={"public_command": slow})
    calls = []

    class SlowSolver:
        def request(self, *args):
            calls.append(1)
            return Action(tool=tool, argv=slow, reason="Controlled solver command deadline")

    options = settings.model_copy(update={"command_timeout": 1, "max_steps": 4})
    run_id = Runner(options, store, SlowSolver()).solve(task)
    run = store.run(run_id)
    assert run["state"] == "EXHAUSTED" and not run["result"]["passed"]
    assert run["result"]["exhaustion_reason"] == "solver_command_timeout"
    assert run["result"]["patch"] is None and run["result"]["image_id"].startswith("sha256:")
    assert len(calls) == 1 and run["result"]["steps"] == 1
    events = store.events(run_id)
    assert any(e["kind"] == "COMMAND_BUDGET_EXHAUSTED" for e in events)
    assert not any(e["kind"] in {"ERROR", "GRADE"} for e in events)
    evolution = Evolution(options, store, model=SlowSolver(), feedback_policy="post_edit_or_budget")
    evidence = evolution.freeze([run_id])[1]
    episode = evidence["episodes"][0]
    assert episode["execution_feedback"]["command_budget_exhausted"]
    assert not episode["execution_feedback"]["step_budget_exhausted"]
    assert evolution.qualifies(episode)
    projection = evolution.project(evidence)["episodes"][0]
    assert projection["observations"][-1]["exit_code"] == 124
    assert projection["public_failed"] is (tool == "test")


@pytest.mark.parametrize(
    "tool,message", [("read", "command_timeout: sandbox destroyed"), ("command", "docker exec failed")]
)
def test_non_solver_deadlines_and_docker_failures_still_stop_as_errors(
    settings, store, monkeypatch, tool, message
):
    task = fixture_task("real-infrastructure", "learn", False)

    class Solver:
        def request(self, *args):
            return Action(
                tool=tool, path=task.editable[0], argv=["python", "-V"], reason="Infrastructure fixture"
            )

    def broken(*args):
        raise RuntimeError(message)

    monkeypatch.setattr(Runner, "execute", broken)
    run_id = Runner(settings, store, Solver()).solve(task)
    assert store.run(run_id)["state"] == "ERROR"
    assert not any(e["kind"] == "COMMAND_BUDGET_EXHAUSTED" for e in store.events(run_id))
