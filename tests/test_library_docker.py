import json

import pytest

from evocoder.contracts import Action, LibraryRoute, Settings
from evocoder.dataset import fixture_task
from evocoder.evolution import Evolution
from evocoder.library_demo import run_demo
from evocoder.runtime import Runner
from evocoder.store import Store
from test_library import seed

pytestmark = pytest.mark.docker


def test_mixed_families_revision_full_regression_and_frozen_final_test(tmp_path):
    root = tmp_path / "demo"
    report = run_demo(root)
    assert report["completed"], report["error"]
    assert report["proposal_count"] == 3 and len(report["pool"]) == 2
    first, second, third = report["timeline"]
    assert first["parent"] is None and not first["gate_passed"]
    assert second["applied_skill"] is None and second["parent"] is None
    assert second["gate_passed"] and third["parent"] == first["candidate"]
    assert third["gate_passed"] and second["candidate"] in third["pool_after"]
    store = Store(root / "state")
    with store.connect() as db:
        record = json.loads(db.execute("SELECT payload FROM events WHERE kind='SUMMARY_VERIFIED'").fetchone()[0])
    assert record["candidate"] == third["candidate"]
    assert store.library_members(report["active_library"], mode="mock", require_published=True) == sorted(
        [second["candidate"], third["candidate"]])
    assert store.active() is None and all(r["result"]["passed"] for r in report["final_runs"])
    assert {r["skill_id"] for r in report["final_runs"]} == {second["candidate"], third["candidate"]}
    assert all(r["purpose"] == "evaluate" for r in report["final_runs"])
    for run in report["final_runs"]:
        with pytest.raises(ValueError, match="dev/test/evaluator"):
            Evolution(Settings(_env_file=None, state_dir=root / "state"), store).freeze([run["id"]])
    assert report["budget"]["spent_usd"] == 0
    assert report["claim"] == "engineering_validation_only"


def test_uncertain_library_route_injects_no_skill_and_consumes_one_step(settings, store):
    key = seed(settings, store, fixture_task("seed", "learn", False))
    calls = []
    class Model:
        def request(self, schema, prompt, data, run_id):
            calls.append(schema)
            if schema is LibraryRoute:
                return LibraryRoute(decision="uncertain", skill_id=None, condition_indices=[], exclusion_indices=[],
                                    task_quotes=[], reason="Insufficient public evidence")
            assert data["skills"] == [] and data["remaining_steps"] == 15
            return Action(tool="finish")
    run_id = Runner(settings, store, Model()).solve(fixture_task("learn", "learn", False),
                                                   use_active=False, skill_ids=[key], candidate=True)
    run = store.run(run_id)
    assert run["skill_id"] is None and run["result"]["model_steps"] == len(calls) == 2
    assert run["result"]["requested_skill_ids"] == [key]
