import httpx
import pytest

from evocoder.contracts import Action
from evocoder.model import Model
from test_budget_model import live_settings, response


def test_unexpected_function_is_billed_and_has_safe_diagnostics(settings, store):
    body = response()
    body["choices"][0]["message"]["tool_calls"][0]["function"]["name"] = "read"
    model = Model(live_settings(settings), store,
                  httpx.MockTransport(lambda request: httpx.Response(200, json=body)))
    from evocoder.errors import ActionRejected
    with pytest.raises(ActionRejected) as rejected:
        model.request(Action, "unit", {}, "run")
    assert "read" in rejected.value.issues[0]["msg"]
    assert rejected.value.issues[0]["loc"] == ["function"]
    assert store.costs(["run"])["cost_usd"] > 0
    record = next(e for e in store.events("run") if e["kind"] == "MODEL_RESPONSE")
    assert record["kind"] == "MODEL_RESPONSE"
    assert record["payload"]["function_names"] == ["read"]
    assert "arguments" not in record["payload"]
