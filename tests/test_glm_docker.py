import json

import httpx
import pytest

from evocoder.contracts import canonical
from evocoder.dataset import fixture_task
from evocoder.model import Model
from evocoder.runtime import Runner
from evocoder.store import Store
from test_glm import glm_settings

pytestmark = pytest.mark.docker


def test_glm_credit_adapter_executes_real_code_and_independent_grading(tmp_path):
    options = glm_settings(tmp_path, unlimited_credits=True, credit_budget=0)
    store = Store(options.state_dir, unit="credits")
    task = fixture_task("glm-wire-contract", "learn", False)
    actions = iter(
        [
            {
                "tool": "write",
                "path": task.editable[0],
                "content": "def normalize(value):\n    return ('' if value is None else str(value)).strip().lower()\n",
            },
            {"tool": "test"},
            {"tool": "finish"},
        ]
    )

    def handler(request):
        assert json.loads(request.content)["thinking"]["type"] == "enabled"
        return httpx.Response(
            200,
            json={
                "usage": {"prompt_tokens": 100, "completion_tokens": 30},
                "choices": [
                    {
                        "finish_reason": "tool_calls",
                        "message": {
                            "tool_calls": [
                                {
                                    "type": "function",
                                    "function": {
                                        "name": "submit_action",
                                        "arguments": canonical(next(actions)),
                                    },
                                }
                            ]
                        },
                    }
                ],
            },
        )

    run_id = Runner(options, store, Model(options, store, httpx.MockTransport(handler))).solve(
        task, use_active=False
    )
    run = store.run(run_id)
    assert run["state"] == "COMPLETE" and run["result"]["passed"]
    assert run["result"]["model_steps"] == 3
    assert run["result"]["cost"]["cost_credits"] == 0.423
    assert "cost_usd" not in run["result"]["cost"]
    assert store.budget()["reserved_credits"] == 0
