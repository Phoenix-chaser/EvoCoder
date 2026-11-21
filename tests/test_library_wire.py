import json

import httpx
import pytest

from evocoder.contracts import LibraryRoute, canonical
from evocoder.errors import RoutingRejected
from evocoder.model import Model
from evocoder.store import Store
from test_budget_model import response
from test_glm import glm_settings


@pytest.mark.parametrize(
    "index,decision,expected",
    [
        (-1, "skip", None),
        (-2, "uncertain", None),
        (0, "apply", "first-artifact-hash"),
        (1, "apply", "second-artifact-hash"),
    ],
)
def test_library_wire_validates_integer_choice_and_runtime_binds_original_id(tmp_path, index, decision, expected):
    options = glm_settings(tmp_path)
    store = Store(options.state_dir, unit="credits")
    arguments = {
        "skill_index": index,
        "condition_indices": [],
        "exclusion_indices": [],
        "task_quotes": [],
        "reason": "wire contract",
    }

    def handler(request):
        body = json.loads(request.content)
        definition = body["tools"][0]["function"]["parameters"]
        assert "skill_id" not in definition["properties"]
        assert "decision" not in definition["properties"]
        assert definition["properties"]["skill_index"]["enum"] == [-2, -1, 0, 1]
        assert "skill_index" in definition["required"]
        data = json.loads(body["messages"][1]["content"])
        assert [x["skill_index"] for x in data["skills"]] == [0, 1]
        assert "first-artifact-hash" not in canonical(body)
        assert "second-artifact-hash" not in canonical(body)
        result = response()
        result["choices"][0]["message"]["tool_calls"][0]["function"].update(
            name="select_library_skill", arguments=canonical(arguments)
        )
        return httpx.Response(200, json=result)

    route = Model(options, store, httpx.MockTransport(handler)).request(
        LibraryRoute,
        "unit",
        {"skills": [{"id": "first-artifact-hash"}, {"id": "second-artifact-hash"}]},
        "run",
    )
    assert route.skill_id == expected and route.decision == decision
    assert store.budget()["reserved_credits"] == 0
    assert any(e["kind"] == "ROUTING_RUNTIME_BINDING" for e in store.events("run"))


@pytest.mark.parametrize(
    "changes",
    [
        dict(decision="skip", skill_index=0),
        dict(decision="apply", skill_index=-1),
        dict(skill_index=2),
        dict(skill_index=-3),
        dict(skill_index=True),
        dict(skill_index="0"),
        dict(skill_index="null"),
        dict(skill_index=None),
        dict(exclusion_indices=[0]),
        dict(skill_id="null"),
        dict(skill_id="first-artifact-hash"),
    ],
)
def test_invalid_library_choice_is_charged_retained_and_never_coerced(tmp_path, changes):
    options = glm_settings(tmp_path)
    store = Store(options.state_dir, unit="credits")
    arguments = dict(
        skill_index=0,
        condition_indices=[],
        exclusion_indices=[],
        task_quotes=[],
        reason="invalid choice",
    )
    arguments.update(changes)
    calls = []

    def handler(request):
        calls.append(1)
        payload = response()
        payload["choices"][0]["message"]["tool_calls"][0]["function"].update(
            name="select_library_skill", arguments=canonical(arguments)
        )
        return httpx.Response(200, json=payload)

    with pytest.raises(RoutingRejected):
        Model(options, store, httpx.MockTransport(handler)).request(
            LibraryRoute,
            "unit",
            {"skills": [{"id": "first-artifact-hash"}, {"id": "second-artifact-hash"}]},
            "run",
        )
    assert len(calls) == 1
    assert store.costs(["run"])["cost_credits"] > 0 and store.budget()["reserved_credits"] == 0
    rejected = next(e for e in store.events("run") if e["kind"] == "MODEL_ARGUMENTS_REJECTED")
    assert json.loads(store.get_artifact(rejected["payload"]["artifact"])["arguments"]) == arguments
    assert not any(e["kind"] == "ROUTING_RUNTIME_BINDING" for e in store.events("run"))
