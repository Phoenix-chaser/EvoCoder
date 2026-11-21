import pytest

from evocoder.contracts import Action, LibraryRoute, canonical, digest
from evocoder.dataset import fixture_task
from evocoder.evolution import Evolution
from evocoder.runtime import Runner
from test_library import seed


@pytest.mark.parametrize("decision", ["apply", "skip"])
def test_fixed_route_replays_same_solver_input_and_verification(settings, store, monkeypatch, decision):
    task = fixture_task("repair", "dev", False)
    key = seed(settings, store, fixture_task("reference", "learn", False))
    checks = []

    class Box:
        image_id = "controlled-image"

        def __init__(self, files, _options, label):
            self.files, self.label = files, label

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

        def export(self, paths):
            return {p: self.files[p] for p in paths}

        def execute(self, argv):
            checks.append((self.label, argv, digest(self.files)))
            return {"exit_code": 0, "timeout": False, "output": "", "seconds": 0.1}

    monkeypatch.setattr("evocoder.runtime.Sandbox", Box)
    calls = []

    class Model:
        def request(self, schema, prompt, data, run_id):
            calls.append((schema, prompt, data))
            if schema is LibraryRoute:
                return LibraryRoute(
                    decision=decision, skill_id=key if decision == "apply" else None,
                    condition_indices=[0] if decision == "apply" else [], exclusion_indices=[],
                    task_quotes=[task.issue] if decision == "apply" else [], reason="Controlled router",
                )
            assert schema is Action
            return Action(tool="finish")

    runner = Runner(settings, store, Model())
    kwargs = {"use_active": False, "purpose": "evaluate", "skill_ids": [key]}
    semantic = store.run(runner.solve(task, **kwargs))
    fixed = store.run(runner.solve(task, **kwargs, fixed_route=decision))
    assert [entry[0] for entry in calls] == [LibraryRoute, Action, Action]
    assert calls[1] == calls[2]
    solver_input = calls[2][2]
    assert solver_input["remaining_steps"] == settings.max_steps - 1
    assert solver_input["skills"] == ([store.get_artifact(key)] if decision == "apply" else [])
    result = fixed["result"]
    assert fixed["skill_id"] == result["applied_skill_id"] == semantic["skill_id"]
    assert fixed["purpose"] == "evaluate" and result["passed"] and semantic["result"]["passed"]
    assert result["model_steps"] == 1
    assert result["budget_steps"] == semantic["result"]["model_steps"] == 2
    assert result["routing"]["reserved_route_steps"] == 1
    assert result["routing"]["eligible_skill_id"] == key and result["routing"]["decision"] == decision
    assert result["routing"]["solver_memory_hash"] == digest({"skills": solver_input["skills"]})
    assert result["routing"]["source"] == "fixed_intervention"
    events = {e["kind"]: e["payload"] for e in store.events(fixed["id"])}
    assert events["SKILL_ROUTE"]["applied_skill_id"] == fixed["skill_id"]
    assert events["START"]["fixed_route"] == decision
    assert [c[1:] for c in checks[:2]] == [c[1:] for c in checks[2:]]
    assert [c[1] for c in checks[:2]] == [task.public_command, task.grader_command]
    assert store.active_library() is None
    with pytest.raises(ValueError, match="dev/test/evaluator"):
        Evolution(settings, store).freeze([fixed["id"]])


@pytest.mark.parametrize("overrides", [
    {"purpose": "solve"}, {"fixed_route": "uncertain"}, {"skill_ids": []},
    {"skill_ids": ["a", "b"]}, {"skill_ids": None}, {"use_active": True},
])
def test_fixed_route_rejects_invalid_interventions_before_creating_run(settings, store, overrides):
    kwargs = dict(use_active=False, purpose="evaluate", skill_ids=["a"], fixed_route="skip")
    with pytest.raises(ValueError):
        Runner(settings, store).solve(fixture_task("learn", "learn", False), **(kwargs | overrides))
    with store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0


def test_fixed_route_refuses_unbound_content_and_overbudget_apply(settings, store):
    task = fixture_task("dev", "dev", False)
    key = seed(settings, store, fixture_task("s", "learn", False))
    skill = store.get_artifact(key)
    limit = len(canonical({"skills": [skill]}).encode()) - 1
    runner = Runner(settings.model_copy(update={"max_memory_bytes": limit}), store)
    kwargs = dict(use_active=False, purpose="evaluate", skill_ids=[key], fixed_route="apply")
    with pytest.raises(ValueError, match="memory budget"):
        runner.solve(task, **kwargs)
    with store.connect(write=True) as db:
        db.execute("UPDATE artifacts SET body=? WHERE id=?", (canonical({**skill, "steps": ["changed"]}), key))
    with pytest.raises(ValueError, match="integrity mismatch"):
        runner.solve(task, **kwargs)
    with store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM runs WHERE purpose='evaluate'").fetchone()[0] == 0
