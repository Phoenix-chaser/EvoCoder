import pytest

from evocoder.contracts import SkillRoute
from evocoder.dataset import fixture_task
from evocoder.errors import RoutingRejected
from evocoder.model import Model
from evocoder.routing import select_skill


def skill():
    return Model.mock_skill({"parent": None, "evidence": {"episodes": [
        {"run_id": "seed", "public_failed": False}]}})


@pytest.mark.parametrize("decision,conditions,exclusions,quotes,applied", [
    ("apply", [0], [], ["Fix optional input normalization"], True),
    ("skip", [], [], [], False), ("uncertain", [], [], [], False),
])
def test_routing_only_reads_public_issue_and_scope(decision, conditions, exclusions, quotes, applied):
    task = fixture_task("route", "learn", False)
    class Router:
        def request(self, schema, system, data, run_id):
            assert schema is SkillRoute and run_id == "run"
            assert set(data["task"]) == {"issue"}
            assert set(data["skill"]) == {"applicability", "exclusions"}
            assert "grader" not in str(data) and "steps" not in data["skill"]
            return SkillRoute(decision=decision, condition_indices=conditions, exclusion_indices=exclusions,
                              task_quotes=quotes, reason="Scope checked against supplied public text")
    assert select_skill(Router(), task, skill(), "run")["applied"] is applied


@pytest.mark.parametrize("conditions,exclusions,quotes", [
    ([7], [], ["Fix optional input normalization"]),
    ([0], [7], ["Fix optional input normalization"]),
    ([0], [0], ["Fix optional input normalization"]),
    ([0], [], ["invented evidence"]), ([], [], ["Fix optional input normalization"]),
    ([0], [], []),
])
def test_unsupported_application_is_rejected(conditions, exclusions, quotes):
    class Router:
        def request(self, *args):
            return SkillRoute(decision="apply", condition_indices=conditions, exclusion_indices=exclusions,
                              task_quotes=quotes, reason="Untrusted model assertion")
    with pytest.raises(RoutingRejected):
        select_skill(Router(), fixture_task("route", "learn", False), skill(), "run")
