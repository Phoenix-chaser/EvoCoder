import json

import httpx
import pytest

from evocoder.contracts import Action, Settings, Skill, canonical
from evocoder.model import Model
from evocoder.store import Store, nanos
from test_budget_model import response


def glm_settings(tmp_path, **changes):
    values = dict(
        _env_file=None,
        mode="live",
        provider="glm",
        model="glm-5.3",
        api_key="secret-glm-test",
        base_url="https://open.bigmodel.cn/api/coding/paas/v4",
        billing_mode="subscription_credits",
        credit_budget=500,
        reasoning_effort="low",
        input_credits_per_million=690,
        cached_input_credits_per_million=170,
        output_credits_per_million=2400,
        state_dir=tmp_path / "glm",
    )
    values.update(changes)
    return Settings(**values)


def test_glm_uses_documented_protocol_and_accounts_credits_without_dollars(tmp_path):
    options = glm_settings(tmp_path)
    store = Store(options.state_dir, unit="credits")

    def handler(request):
        assert request.url.path == "/api/coding/paas/v4/chat/completions"
        body = json.loads(request.content)
        assert body["thinking"] == {"type": "enabled"}
        assert body["reasoning_effort"] == "low" and body["tool_choice"] == "auto"
        payload = response()
        payload["usage"]["prompt_tokens_details"] = {"cached_tokens": 80}
        return httpx.Response(200, json=payload)

    action = Model(options, store, httpx.MockTransport(handler)).request(Action, "unit", {}, "run")
    assert action.tool == "finish"
    assert store.costs(["run"]) == {"cost_credits": 0.0994, "reserved_credits": 0, "calls": 1}
    assert "spent_usd" not in store.budget() and store.budget()["cap_credits"] == 500


@pytest.mark.parametrize("cached", [-1, 101, True, "80", None])
def test_glm_invalid_cache_usage_is_priced_as_missed_input(tmp_path, cached):
    options = glm_settings(tmp_path)
    store = Store(options.state_dir, unit="credits")
    payload = response()
    payload["usage"]["prompt_tokens_details"] = {"cached_tokens": cached}
    Model(options, store, httpx.MockTransport(lambda r: httpx.Response(200, json=payload))).request(
        Action, "unit", {}, "run"
    )
    assert store.costs(["run"])["cost_credits"] == 0.141


def test_subscription_mode_rejects_standard_billing_endpoint_and_disabled_glm_thinking(tmp_path):
    with pytest.raises(ValueError, match="Coding endpoint"):
        glm_settings(tmp_path, base_url="https://open.bigmodel.cn/api/paas/v4").validate_live()
    with pytest.raises(ValueError, match="thinking enabled"):
        glm_settings(tmp_path, disable_thinking=True).validate_live()
    with pytest.raises(ValueError, match="positive finite"):
        glm_settings(tmp_path, credit_budget=0).validate_live()


def test_credit_ledger_cannot_relabel_or_detach_from_a_dollar_ledger(tmp_path):
    usd = Store(tmp_path / "usd")
    call = usd.reserve("old", nanos(1), 10)
    usd.settle(call, 7, {})
    before = usd.budget()
    with pytest.raises(ValueError, match="another unit"):
        Store(usd.root, unit="credits")
    credits = Store(tmp_path / "credits", unit="credits")
    with pytest.raises(ValueError, match="units must match"):
        Store(tmp_path / "branch", ledger=usd, unit="credits")
    with pytest.raises(ValueError, match="ledger unit"):
        Model(glm_settings(tmp_path), usd)
    assert usd.budget() == before and credits.unit == "credits"


def test_explicit_unlimited_local_credits_still_settles_and_keeps_uncertain_reservations(tmp_path):
    options = glm_settings(tmp_path, credit_budget=0, unlimited_credits=True)
    store = Store(options.state_dir, unit="credits")
    Model(options, store, httpx.MockTransport(lambda r: httpx.Response(200, json=response()))).request(
        Action, "unit", {}, "one"
    )
    assert store.budget()["cap_credits"] is None
    assert store.budget()["spent_credits"] == 0.141

    attempts = []

    def lost(request):
        attempts.append(1)
        raise httpx.ReadTimeout("controlled response loss")

    with pytest.raises(httpx.ReadTimeout):
        Model(options, store, httpx.MockTransport(lost)).request(Action, "unit", {}, "two")
    assert len(attempts) == 1 and store.budget()["reserved_credits"] > 0
    assert store.budget()["spent_credits"] == 0.141


def test_explicit_glm_file_never_inherits_another_providers_environment_key(tmp_path, monkeypatch):
    monkeypatch.setenv("EVOCODER_API_KEY", "other-provider-secret")
    path = tmp_path / "config"
    path.write_text("EVOCODER_API_KEY=\nEVOCODER_PROVIDER=glm\n")
    assert Settings.from_env_file(path).api_key == ""
    path.write_text("EVOCODER_PROVIDER=glm\n")
    with pytest.raises(ValueError, match="must contain"):
        Settings.from_env_file(path)


@pytest.mark.parametrize("parent", [None, "exact-parent-hash"])
@pytest.mark.parametrize("tamper", [False, True])
def test_skill_content_contract_binds_runtime_lineage_and_rejects_model_metadata(tmp_path, parent, tamper):
    from pydantic import ValidationError

    options = glm_settings(tmp_path)
    store = Store(options.state_dir, unit="credits")
    skill = Model.mock_skill(
        {"parent": None, "evidence": {"episodes": [{"run_id": "evidence", "public_failed": False}]}}
    )
    generated = skill.model_dump(exclude={"parent"})
    if tamper:
        generated["parent"] = "null"

    def handler(request):
        body = json.loads(request.content)
        definition = body["tools"][0]["function"]["parameters"]
        assert "parent" not in definition["properties"]
        assert "parent" not in definition["required"]
        assert definition["additionalProperties"] is False
        payload = response()
        call = payload["choices"][0]["message"]["tool_calls"][0]["function"]
        call.update(name="submit_skill", arguments=canonical(generated))
        return httpx.Response(200, json=payload)

    model = Model(options, store, httpx.MockTransport(handler))
    data = {"parent": {"id": parent} if parent else None}
    if tamper:
        with pytest.raises(ValidationError, match="Extra inputs"):
            model.request(Skill, "unit", data, "run")
        assert any(e["kind"] == "MODEL_ARGUMENTS_REJECTED" for e in store.events("run"))
    else:
        result = model.request(Skill, "unit", data, "run")
        assert result.parent == parent
        assert result.model_dump(exclude={"parent"}) == generated
        event = next(e for e in store.events("run") if e["kind"] == "PROPOSAL_RUNTIME_BINDING")
        assert event["payload"]["parent"] == parent
        assert store.get_artifact(event["payload"]["content_artifact"]) == generated
