import json

import httpx
import pytest

from evocoder.contracts import Action, Task, digest
from evocoder.errors import ActionRejected
from evocoder.model import Model
from evocoder.runtime import Runner
from test_budget_model import live_settings
from test_recovery_docker import reply


def example_task():
    return Task(
        id="toggle-fixture", repository="controlled/fixture", source_group="controlled:toggle",
        split="learn", issue="Fix the enabled and disabled outputs.",
        files={"value.py": "value = 0\n", "config.txt": "enabled\r\ndisabled\r\n",
               "check.py": "from value import value\nassert value > 0\n"},
        editable=["value.py", "config.txt"], public_command=["python", "check.py"],
        grader_files={"private.py": "from value import value\nassert value == 1\n"},
        grader_command=["python", "private.py"],
    )


def test_disabled_checkpoint_preserves_wire_and_settings_contract(settings, store):
    options = live_settings(settings)
    def provider(request):
        parameters = json.loads(request.content)["tools"][0]["function"]["parameters"]
        assert "restore_checkpoint" not in parameters["properties"]["tool"]["enum"]
        assert "checkpoint_id" not in parameters["properties"]
        return httpx.Response(200, json=reply({"tool": "restore_checkpoint"}))

    assert "public_checkpoints" not in options.public()
    with pytest.raises(ActionRejected):
        Model(options, store, httpx.MockTransport(provider)).request(Action, "solve", {}, "test-disabled")


@pytest.mark.docker
def test_clean_public_checks_green_then_break_then_restore_is_run_local(settings, store):
    task = example_task()
    options = live_settings(settings).model_copy(update={"public_checkpoints": True})
    initial = {p: task.files[p] for p in task.editable}
    first = initial | {"value.py": "value = 1\n"}
    second = initial | {"value.py": "value = 2\n"}
    foreign = store.artifact("patch", initial | {"value.py": "value = 999\n"})
    actions = iter([
        {"tool": "command", "argv": ["python", "-c", "open('check.py','w').close();open('scratch','w').close()"]},
        {"tool": "test"},  # Clean frozen tests reject the original despite dirty test files in solver.
        {"tool": "write", "path": "value.py", "content": first["value.py"]},
        {"tool": "command", "argv": task.public_command},
        {"tool": "write", "path": "value.py", "content": second["value.py"]},
        {"tool": "test"},
        {"tool": "write", "path": "value.py", "content": initial["value.py"]},
        {"tool": "test"},
        {"tool": "restore_checkpoint", "checkpoint_id": foreign},
        {"tool": "restore_checkpoint", "checkpoint_id": digest(first)},
        {"tool": "command", "argv": ["python", "-c", "from pathlib import Path;assert not Path('scratch').exists();assert Path('check.py').stat().st_size>0"]},
        {"tool": "finish"},
    ])
    def provider(request):
        body = json.loads(request.content)
        assert "restore_checkpoint" in body["tools"][0]["function"]["parameters"]["properties"]["tool"]["enum"]
        data = json.loads(body["messages"][1]["content"])
        action = next(actions)
        if action["tool"] == "restore_checkpoint":
            assert data["latest_public_checkpoint"] == digest(second)
        return httpx.Response(200, json=reply(action))

    solved = Runner(options, store, Model(options, store, httpx.MockTransport(provider))).solve(task, use_active=False)
    run = store.run(solved)
    assert run["state"] == "COMPLETE" and run["result"]["passed"]
    assert run["result"]["patch"] == digest(first) and run["result"]["rejected_actions"] == 1
    assert run["config"]["public_checkpoints"] is True
    events = store.events(solved)
    tested = [e["payload"] for e in events if e["kind"] == "CHECKPOINT_TESTED"]
    assert [x["exit_code"] for x in tested] == [1, 0, 0, 1]
    assert [x["patch_hash"] for x in tested] == [digest(initial), digest(first), digest(second), digest(initial)]
    saved = [e["payload"] for e in events if e["kind"] == "PUBLIC_CHECKPOINT"]
    assert saved == [tested[1], tested[2]]
    restored, = [e["payload"] for e in events if e["kind"] == "CHECKPOINT_RESTORED"]
    assert restored["previous_patch_hash"] == digest(initial)
    assert restored["restored_patch_hash"] == restored["patch_hash"] == digest(first)
    assert restored["argv"] == task.public_command and restored["exit_code"] == 0 and not restored["timeout"]
    assert store.get_artifact(restored["patch_hash"]) == first
    for index, event in enumerate(events):
        if event["kind"] in {"CHECKPOINT_TESTED", "CHECKPOINT_RESTORED"}:
            observation = next(e["payload"]["observation"] for e in events[index+1:] if e["kind"] == "OBSERVATION")
            assert event["payload"]["output_hash"] == digest(observation["output"])
    obs = [e["payload"]["observation"] for e in events if e["kind"] == "OBSERVATION"]
    assert obs[-1]["exit_code"] == 0


@pytest.mark.docker
def test_controlled_learning_application_keeps_green_when_later_steps_exhaust(settings, store):
    from evocoder.dataset import fixture_task
    from test_library import seed

    task = example_task()
    options = settings.model_copy(update={"public_checkpoints": True, "max_steps": 4})
    skill = seed(options, store, fixture_task("learning-evidence", "learn", False))
    actions = iter([
        Action(tool="write", path="value.py", content="value = 1\n"),
        Action(tool="test"),
        Action(tool="write", path="value.py", content="value = 0\n"),
    ])

    class Script:
        def request(self, schema, prompt, data, run_id):
            assert schema is Action and data["skills"] == [store.get_artifact(skill)]
            return next(actions)

    runner = Runner(options, store, Script())
    kwargs = dict(purpose="solve", candidate=True, use_active=False, skill_ids=[skill], fixed_route="apply")
    for rejected in ({"candidate": False}, {"use_active": True}, {"fixed_route": "skip"}):
        with pytest.raises(ValueError):
            runner.solve(task, **(kwargs | rejected))
    with pytest.raises(ValueError):
        runner.solve(task.model_copy(update={"split": "dev"}), **kwargs)

    key = runner.solve(task, **kwargs)
    run = store.run(key)
    assert run["state"] == "EXHAUSTED" and not run["result"]["passed"] and run["result"]["patch"] is None
    assert run["purpose"] == "solve" and run["skill_id"] == skill
    assert run["result"]["budget_steps"] == 4 and run["result"]["model_steps"] == 3
    assert run["result"]["routing"]["source"] == "fixed_intervention"
    green, = [e["payload"] for e in store.events(key) if e["kind"] == "PUBLIC_CHECKPOINT"]
    assert store.get_artifact(green["patch_hash"])["value.py"] == "value = 1\n"
    assert not any(e["kind"] in {"GRADE", "CHECKPOINT_RESTORED"} for e in store.events(key))
