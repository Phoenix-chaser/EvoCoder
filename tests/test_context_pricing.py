import json

import httpx
import pytest

from evocoder.contracts import Action, canonical
from evocoder.model import Model
from test_budget_model import live_settings, response


@pytest.mark.parametrize("hit,miss,expected", [(80, 20, 0.000088), (80, 19, 0.00016), (-1, 101, 0.00016)])
def test_cache_discount_only_applies_to_consistent_provider_usage(settings, store, hit, miss, expected):
    def handler(request):
        payload = response()
        payload["usage"].update({"prompt_cache_hit_tokens": hit, "prompt_cache_miss_tokens": miss})
        return httpx.Response(200, json=payload)

    options = live_settings(settings).model_copy(update={"cached_input_price_per_million": 0.1})
    Model(options, store, httpx.MockTransport(handler)).request(Action, "unit", {}, "r")
    assert store.costs(["r"])["cost_usd"] == expected
    assert store.budget()["reserved_usd"] == 0


def test_history_projection_fits_input_without_mutating_executed_evidence(settings, store):
    history = [
        {"action": {"tool": "read"}, "observation": {"exit_code": 0, "output": str(i) * 20000}}
        for i in range(6)
    ]
    original = canonical(history)
    seen = []

    def handler(request):
        data = json.loads(json.loads(request.content)["messages"][1]["content"])
        assert data["history_compaction"]["original_entries"] == 6
        assert data["history_compaction"]["level"] >= 2
        assert len(request.content) <= 20000  # HTTPX serialization may differ from canonical preflight JSON.
        seen.append(data)
        return httpx.Response(200, json=response())

    options = live_settings(settings).model_copy(update={"max_input_bytes": 16384})
    Model(options, store, httpx.MockTransport(handler)).request(Action, "unit", {"history": history}, "r")
    assert canonical(history) == original and len(seen) == 1
    assert any(e["kind"] == "MODEL_INPUT_COMPACTED" for e in store.events("r"))


@pytest.mark.parametrize("failure", ["length", "function"])
def test_settled_bad_auxiliary_route_omits_memory_without_an_extra_http_attempt(settings, store, failure):
    from evocoder.contracts import LibraryRoute
    from evocoder.errors import RoutingRejected

    calls = []

    def handler(request):
        calls.append(1)
        payload = response()
        payload["choices"][0]["message"]["tool_calls"][0]["function"]["name"] = "wrong_router"
        if failure == "length":
            payload["choices"][0]["finish_reason"] = "length"
        return httpx.Response(200, json=payload)

    with pytest.raises(RoutingRejected):
        Model(live_settings(settings), store, httpx.MockTransport(handler)).request(
            LibraryRoute, "unit", {"skills": [{"id": "one"}]}, "r"
        )
    assert len(calls) == 1 and store.budget()["reserved_usd"] == 0
