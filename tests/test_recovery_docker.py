import json

import httpx
import pytest

from evocoder.contracts import SkillRoute, canonical
from evocoder.dataset import fixture_task
from evocoder.evolution import Evolution
from evocoder.model import Model
from evocoder.runtime import Runner
from test_budget_model import live_settings

pytestmark = pytest.mark.docker


def reply(body, name="submit_action"):
    return {
        "usage": {"prompt_tokens": 100, "completion_tokens": 30},
        "choices": [
            {
                "finish_reason": "tool_calls",
                "message": {
                    "tool_calls": [
                        {"type": "function", "function": {"name": name, "arguments": canonical(body)}}
                    ]
                },
            }
        ],
    }


def register_skill(store):
    skill = Model.mock_skill(
        {"parent": None, "evidence": {"episodes": [{"run_id": "unit-seed", "public_failed": False}]}}
    )
    key = store.artifact("skill", skill.model_dump())
    evidence = store.artifact("frozen_evidence", {"episodes": []})
    with store.connect(write=True) as db:
        db.execute("INSERT INTO skills VALUES(?,NULL,?,'CANDIDATE')", (key, evidence))
    return key


@pytest.mark.parametrize("failure_kind", ["arguments", "function", "length", "write_path", "edit_path"])
def test_paid_argument_failure_returns_feedback_and_finishes_within_same_budget(
    settings, store, failure_kind
):
    task = fixture_task("recover-read-limit", "learn", False)
    actions = [
        {"tool": "read", "path": task.editable[0], "line_count": 240},
        {
            "tool": "write",
            "path": task.editable[0],
            "content": "def normalize(value):\n    return ('' if value is None else str(value)).strip().lower()\n",
        },
        {"tool": "test"},
        {"tool": "finish"},
    ]
    if failure_kind == "write_path":
        actions[0] = {"tool": "write", "path": "outside.py", "content": "unapproved change"}
    if failure_kind == "edit_path":
        actions[0] = {"tool": "edit", "path": "outside.py", "old_text": "before", "new_text": "after"}
    requests = []

    def handler(request):
        body = json.loads(request.content)
        data = json.loads(body["messages"][1]["content"])
        if len(requests) == 1:
            assert data["remaining_steps"] == 3
            assert data["history"][-1]["action"]["tool"] == "rejected"
            field = {"arguments": "line_count", "function": "function", "length": "finish_reason",
                     "write_path": "not_editable", "edit_path": "not_editable"}[
                failure_kind
            ]
            assert field in data["history"][-1]["observation"]["output"]
        requests.append(body)
        name = "search" if failure_kind == "function" and len(requests) == 1 else "submit_action"
        payload = reply(actions[len(requests) - 1], name)
        if failure_kind == "length" and len(requests) == 1:
            payload["choices"][0]["finish_reason"] = "length"
        return httpx.Response(200, json=payload)

    options = live_settings(settings).model_copy(update={"max_steps": 4})
    run_id = Runner(options, store, Model(options, store, httpx.MockTransport(handler))).solve(task)
    run = store.run(run_id)
    assert run["state"] == "COMPLETE"
    assert run["result"]["model_steps"] == 4 and run["result"]["steps"] == 2
    assert run["result"]["rejected_actions"] == 1 and len(requests) == 4
    assert store.costs([run_id])["cost_usd"] == 0.00064
    assert store.budget()["reserved_usd"] == 0


@pytest.mark.parametrize("recover,expected,calls", [(True, "EXHAUSTED", 4), (False, "ERROR", 1)])
def test_invalid_arguments_cannot_create_unbounded_retries_or_execute_tools(
    settings, store, monkeypatch, recover, expected, calls
):
    count = []
    task = fixture_task("repeated-invalid", "learn", False)

    def handler(request):
        count.append(1)
        return httpx.Response(200, json=reply({"tool": "read", "path": task.editable[0], "line_count": 240}))

    def forbidden(*args):
        raise AssertionError("rejected arguments must never reach tool execution")

    monkeypatch.setattr(Runner, "execute", forbidden)
    options = live_settings(settings).model_copy(update={"max_steps": 4, "recover_actions": recover})
    run_id = Runner(options, store, Model(options, store, httpx.MockTransport(handler))).solve(task)
    run = store.run(run_id)
    assert run["state"] == expected
    assert run["result"]["steps"] == 0 and run["result"]["model_steps"] == calls
    assert len(count) == calls and store.costs([run_id])["calls"] == calls
    assert store.budget()["reserved_usd"] == 0


@pytest.mark.parametrize("decision,applied", [("skip", False), ("apply", True)])
def test_effective_skill_controls_solver_context_and_parent_failure_provenance(
    settings, store, decision, applied
):
    key = register_skill(store)
    task = fixture_task("route-provenance", "learn", False)
    count = []

    def handler(request):
        body = json.loads(request.content)
        data = json.loads(body["messages"][1]["content"])
        count.append(1)
        if body["tool_choice"]["function"]["name"] == "select_skill":
            return httpx.Response(
                200,
                json=reply(
                    SkillRoute(
                        decision=decision,
                        condition_indices=[0] if applied else [],
                        exclusion_indices=[],
                        task_quotes=["Fix optional input normalization"] if applied else [],
                        reason="Explicit unit routing decision",
                    ).model_dump(),
                    "select_skill",
                ),
            )
        assert bool(data["skills"]) is applied
        assert data["remaining_steps"] == 3  # The routing call used a model step.
        return httpx.Response(200, json=reply({"tool": "finish"}))

    options = live_settings(settings).model_copy(update={"max_steps": 4})
    run_id = Runner(options, store, Model(options, store, httpx.MockTransport(handler))).solve(
        task, key, use_active=False, candidate=True
    )
    run = store.run(run_id)
    assert run["state"] == "FAILED" and run["result"]["public_failed"]
    assert run["skill_id"] == (key if applied else None)
    assert run["result"]["requested_skill_id"] == key
    assert len(count) == run["result"]["model_steps"] == 2
    evo = Evolution(options, store)
    if applied:
        assert evo.freeze([run_id], key)[1]["episodes"][0]["skill_id"] == key
    else:
        with pytest.raises(ValueError, match="applying the parent"):
            evo.freeze([run_id], key)


def test_uncertain_routing_http_response_stops_without_retry_or_solver_fallback(settings, store):
    key = register_skill(store)
    requests = []

    def handler(request):
        requests.append(1)
        raise httpx.ReadTimeout("simulated ambiguous routing response")

    options = live_settings(settings)
    run_id = Runner(options, store, Model(options, store, httpx.MockTransport(handler))).solve(
        fixture_task("route-timeout", "learn", False), key, use_active=False, candidate=True
    )
    assert store.run(run_id)["state"] == "ERROR"
    assert len(requests) == 1 and store.budget()["reserved_usd"] > 0
