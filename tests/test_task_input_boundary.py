import json

import httpx
import pytest

from evocoder.comparison import require_current_model_input
from evocoder.contracts import MODEL_TASK_VIEW, canonical
from evocoder.dataset import fixture_task
from evocoder.evolution import Evolution
from evocoder.model import Model
from evocoder.runtime import Runner
from test_budget_model import live_settings, response


def test_solver_and_replayed_proposal_keep_control_metadata_off_wire(settings, store, monkeypatch):
    locator = "controller-only::src/example.py::fixed_scope"
    task = fixture_task("fixed-commit-id-marker", "learn", False).model_copy(
        update={"source_group": locator, "repository": "fixture/input-boundary"}
    )
    requests = []

    class ReadSandbox:
        image_id = "controlled-image"

        def __init__(self, *args):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def read(self, *args):
            return {"exit_code": 0, "output": "large public observation\n" * 1000}

    def handler(request):
        wire = json.loads(request.content)
        data = json.loads(wire["messages"][1]["content"])
        assert locator not in request.content.decode()
        assert task.id not in request.content.decode()
        assert "private-legacy-marker" not in request.content.decode()
        requests.append(data)
        reply = response()
        call = reply["choices"][0]["message"]["tool_calls"][0]["function"]
        if wire["tool_choice"]["function"]["name"] == "submit_action":
            assert data["task"]["editable"] == task.editable
            assert data["task"]["public_command"] == task.public_command
            call["arguments"] = canonical({"tool": "read", "path": task.editable[0]})
        else:
            episode = data["evidence"]["episodes"][0]
            assert episode["task"]["issue"] == task.issue
            call["name"] = "submit_skill"
            call["arguments"] = Model.mock_skill(data).model_dump_json(exclude={"parent"})
        return httpx.Response(200, json=reply)

    monkeypatch.setattr("evocoder.runtime.Sandbox", ReadSandbox)
    config = live_settings(settings).model_copy(update={"max_steps": 4, "max_input_bytes": 20_000})
    model = Model(config, store, httpx.MockTransport(handler))
    run_id = Runner(config, store, model).solve(task, use_active=False)
    run = store.run(run_id)
    assert run["state"] == "EXHAUSTED", run["result"]["error"]
    assert len(requests) == 4 and any("history_compaction" in data for data in requests)
    start = store.events(run_id)[0]["payload"]
    assert start["model_task_view"] == MODEL_TASK_VIEW and start["task"]["source_group"] == locator
    require_current_model_input(store, [run_id])

    # Historical records used the same local view on the wire. Replay must project them too.
    start.pop("model_task_view")
    start["task"]["grader_command"] = ["private-legacy-marker"]
    with store.connect(write=True) as db:
        db.execute("UPDATE events SET payload=? WHERE run_id=? AND kind='START'", (canonical(start), run_id))
    with pytest.raises(ValueError, match="acquire fresh evidence"):
        require_current_model_input(store, [run_id])
    proposal = Evolution(config, store, model).propose([run_id])
    assert store.get_artifact(proposal)["evidence_refs"] == [run_id]
    assert len(requests) == 5
