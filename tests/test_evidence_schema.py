import httpx

from evocoder.contracts import Skill
from evocoder.model import Model
from test_budget_model import live_settings


def test_proposal_schema_enumerates_only_exact_allowed_evidence(settings, store):
    import json

    example = Model.mock_skill(
        {"parent": None, "evidence": {"episodes": [{"run_id": "allowed-run", "public_failed": False}]}}
    )

    def handler(request):
        body = json.loads(request.content)
        definition = body["tools"][0]["function"]["parameters"]
        assert definition["properties"]["evidence_refs"]["items"]["enum"] == ["allowed-run"]
        return httpx.Response(
            200,
            json={
                "usage": {"prompt_tokens": 10, "completion_tokens": 10},
                "choices": [
                    {
                        "finish_reason": "tool_calls",
                        "message": {
                            "tool_calls": [
                                {
                                    "function": {
                                        "name": "submit_skill",
                                        "arguments": example.model_dump_json(exclude={"parent"}),
                                    }
                                }
                            ]
                        },
                    }
                ],
            },
        )

    result = Model(live_settings(settings), store, httpx.MockTransport(handler)).request(
        Skill, "unit", {"allowed_evidence_refs": ["allowed-run"]}, "proposal"
    )
    assert result.evidence_refs == ["allowed-run"]
