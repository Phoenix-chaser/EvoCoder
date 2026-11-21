import json
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest

from evocoder.contracts import Action, Settings, canonical
from evocoder.model import Model
from evocoder.store import nanos


def live_settings(settings):
    return Settings(_env_file=None, state_dir=settings.state_dir, mode="live",
                    base_url="https://unit.example/v1", api_key="secret-unit-key", model="unit-model",
                    budget_usd=1, input_price_per_million=1, output_price_per_million=2)


def response(calls=1, finish="tool_calls"):
    return {"usage": {"prompt_tokens": 100, "completion_tokens": 30}, "choices": [{
        "finish_reason": finish, "message": {"tool_calls": [{"type": "function", "function": {
            "name": "submit_action", "arguments": canonical({"tool": "finish", "reason": "Done"})}}
            for _ in range(calls)]}}]}


def test_atomic_cumulative_budget_and_immutable_cap(store):
    def reserve(_):
        try:
            return store.reserve("r", 10, 3)
        except ValueError:
            return None
    with ThreadPoolExecutor(max_workers=8) as pool:
        ids = [x for x in pool.map(reserve, range(12)) if x]
    assert len(ids) == 3
    assert store.budget()["reserved_usd"] == 9e-9
    with pytest.raises(ValueError, match="immutable"):
        store.reserve("r", 100, 1)
    store.settle(ids[0], 2, {})
    with pytest.raises(ValueError, match="open reservation"):
        store.settle(ids[0], 2, {})


def test_overrun_seals_scope(store):
    key = store.reserve("r", 100, 10)
    store.settle(key, 11, {})
    assert store.budget()["blocked"]
    with pytest.raises(ValueError, match="exhausted"):
        store.reserve("r", 100, 1)


def test_live_protocol_and_exact_usage_settlement(settings, store):
    def handler(request):
        assert request.url.path == "/v1/chat/completions"
        body = json.loads(request.content)
        assert body["tool_choice"]["function"]["name"] == "submit_action"
        assert "thinking" not in body
        return httpx.Response(200, json=response())
    model = Model(live_settings(settings), store, httpx.MockTransport(handler))
    action = model.request(Action, "unit", {}, "r")
    assert action.tool == "finish"
    assert store.costs(["r"])["cost_usd"] == 0.00016
    assert store.budget()["reserved_usd"] == 0
    assert "secret-unit-key" not in canonical(store.budget())


@pytest.mark.parametrize("calls,finish", [(0, "tool_calls"), (2, "tool_calls"), (1, "length")])
def test_invalid_output_still_accounts_paid_usage(settings, store, calls, finish):
    model = Model(live_settings(settings), store,
                  httpx.MockTransport(lambda req: httpx.Response(200, json=response(calls, finish))))
    with pytest.raises(ValueError, match="model_contract"):
        model.request(Action, "unit", {}, "r")
    assert store.costs(["r"])["cost_usd"] == 0.00016
    assert store.budget()["reserved_usd"] == 0


def test_uncertain_response_keeps_reservation_without_retry(settings, store):
    calls = []
    def handler(request):
        calls.append(1)
        raise httpx.ReadTimeout("simulated loss")
    model = Model(live_settings(settings), store, httpx.MockTransport(handler))
    with pytest.raises(httpx.ReadTimeout):
        model.request(Action, "unit", {}, "r")
    assert len(calls) == 1
    assert store.budget()["reserved_usd"] > 0
    assert store.budget()["spent_usd"] == 0


def test_small_input_budget_rejects_before_spending(settings, store):
    model = Model(live_settings(settings).model_copy(update={"max_input_bytes": 4096}), store)
    with pytest.raises(ValueError, match="input exceeds"):
        model.request(Action, "x" * 10000, {}, "r")
    assert not store.budget()["configured"]


def test_decimal_money_rounds_up():
    assert nanos("0.0000000001") == 1
