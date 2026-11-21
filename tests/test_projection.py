import pytest

from evocoder.evolution import Evolution


def test_bounded_projection_keeps_latest_failure_even_after_other_commands():
    observations = [{"kind": "START", "payload": {"task": {"issue": "x" * 3000}}}]
    for i in range(8):
        observations.append({"kind": "OBSERVATION", "payload": {
            "action": {"tool": "test" if i == 1 else "command", "argv": ["python"]},
            "observation": {"exit_code": 1 if i == 1 else 0, "output": str(i) * 10000}}})
    evidence = {"parent_id": "v1", "episodes": [{"run_id": "r", "skill_id": "v1",
                                                 "public_failed": True, "events": observations}]}
    projected = Evolution.project(evidence)["episodes"][0]
    assert any(o["tool"] == "test" and o["exit_code"] == 1 for o in projected["observations"])
    assert all(len(o["output"]) < 1900 for o in projected["observations"])
    assert len(projected["task"]["issue"]) == 1800


def test_projection_preserves_first_and_latest_episodes():
    episodes = [{"run_id": str(i), "skill_id": None, "public_failed": False,
                 "events": [{"kind": "START", "payload": {"task": {"issue": "x"}}}]}
                for i in range(12)]
    result = Evolution.project({"parent_id": None, "episodes": episodes})
    assert [e["run_id"] for e in result["episodes"]] == ["0", "7", "8", "9", "10", "11"]


@pytest.mark.parametrize("maximum,actual", [(-1, 0), (1, -1)])
def test_negative_accounting_cannot_create_credit(store, maximum, actual):
    if maximum < 0:
        with pytest.raises(ValueError, match="positive integer"):
            store.reserve("r", 100, maximum)
    else:
        call = store.reserve("r", 100, maximum)
        with pytest.raises(ValueError, match="nonnegative"):
            store.settle(call, actual, {})
