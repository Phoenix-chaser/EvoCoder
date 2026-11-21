import pytest

from evocoder.contracts import Action
from evocoder.dataset import fixture_task
from evocoder.memory import freeze_pool
from evocoder.model import Model
from evocoder.runtime import Runner

pytestmark = pytest.mark.docker


def test_rag_solver_receives_only_bounded_frozen_public_experience(settings, store):
    old = Runner(settings, store).solve(fixture_task("historical", "learn", False), use_active=False)
    pool = freeze_pool(store, [old], "mock")
    seen = []

    class Reader:
        def request(self, schema, prompt, data, run_id):
            assert schema is Action
            assert data["skills"] == [] and data["historical_experience"][0]["run_id"] == old
            assert "grader" not in str(data["historical_experience"])
            seen.append(data["remaining_steps"])
            return Model.mock_action(data)

    run_id = Runner(settings, store, Reader()).solve(
        fixture_task("unseen", "test", False), use_active=False, purpose="evaluate", experience_pool_id=pool
    )
    run = store.run(run_id)
    assert run["result"]["passed"] and run["result"]["experience_pool_id"] == pool
    assert run["skill_id"] is None and run["result"]["model_steps"] == len(seen) == 4
    assert any(e["kind"] == "EXPERIENCE_RETRIEVED" for e in store.events(run_id))
