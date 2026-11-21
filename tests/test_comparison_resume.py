import json

import httpx
import pytest

from evocoder import comparison
from evocoder.contracts import Action, digest
from evocoder.dataset import fixture_task
from evocoder.memory import freeze_pool
from evocoder.model import Model
from evocoder.reservations import audit_historical_reservations, open_calls
from evocoder.store import Store
from test_budget_model import live_settings, response


@pytest.mark.parametrize("failure", ["http_500", "process_exit"])
def test_interrupted_final_matrix_only_dispatches_unstarted_cells(settings, store, monkeypatch, tmp_path, failure):
    options = live_settings(settings)
    root = tmp_path / "study"
    arms = {"A": "common", "B": "B", "C": "C", "D": "D"}
    stores = {arm: Store(root / name, ledger=store) for arm, name in arms.items()}
    task = fixture_task("test", "test", False)
    learn = fixture_task("history", "learn", False)
    with stores["A"].connect(write=True) as db:
        db.execute("INSERT INTO runs VALUES(?,?,?,?,?,?,?,?,?,?,?)", (
            "history", learn.id, learn.split, digest(learn.model_dump()), None, "live", "solve",
            "COMPLETE", 0, "{}", "{}",
        ))
    stores["A"].event("history", "START", {"task": learn.public()})
    pool = freeze_pool(stores["A"], ["history"], "live")
    store.configure_budget(1_000_000_000)
    protocol = {"repeats": 1, "workers": 1, "budget_before": store.budget()}
    frozen = {
        "protocol_hash": digest(protocol),
        "knowledge_hashes": {arm: comparison.frozen_knowledge(state) for arm, state in stores.items()},
        "library_ids": dict.fromkeys(arms),
        "experience_pool": pool,
        "open_calls": [],
        "phases": [],
        "test_preflight": None,
    }
    comparison.dump(root / "protocol.json", protocol)
    comparison.dump(root / "started.json", {"protocol_hash": digest(protocol)})
    comparison.dump(root / "frozen-test-state.json", frozen)
    requests = []

    class RecordedRunner:
        def __init__(self, settings, state):
            self.settings, self.state = settings, state

        def solve(self, task, *, run_id, **kwargs):
            with self.state.connect(write=True) as db:
                db.execute("INSERT INTO runs VALUES(?,?,?,?,?,?,?,?,?,?,?)", (
                    run_id, task.id, task.split, digest(task.model_dump()), None,
                    "live", "evaluate", "RUNNING", 0, None, "{}",
                ))

            def handler(request):
                requests.append((self.state.root.name, run_id))
                if self.state.root.name == "B":
                    if failure == "process_exit":
                        raise SystemExit("process stopped before recording an outcome")
                    return httpx.Response(500)
                return httpx.Response(200, json=response())

            try:
                Model(self.settings, self.state, httpx.MockTransport(handler)).request(Action, "unit", {}, run_id)
            except RuntimeError as exc:
                self.state.finish(run_id, "ERROR", {
                    "passed": False, "error": str(exc), "cost": self.state.costs([run_id]),
                })
            else:
                self.state.finish(run_id, "COMPLETE", {"passed": True, "cost": self.state.costs([run_id])})
            return run_id

    monkeypatch.setattr(comparison, "Runner", RecordedRunner)
    expected_error = SystemExit if failure == "process_exit" else RuntimeError
    with pytest.raises(expected_error):
        comparison.evaluate_test_matrix(options, root, protocol, store, stores, [task], frozen)
    assert [arm for arm, _ in requests] == ["common", "B"]

    # A stop after the run committed but before the journal row should recover the recorded result.
    success = root / "cells/test-0-A.json"
    a_row = json.loads(success.read_text())
    intent = {key: a_row[key] for key in ("task_id", "repeat", "arm", "run_id")}
    comparison.dump(success, {**intent, "state": "started"})
    pending = open_calls(store)
    result = comparison.resume_test_matrix(options, root, protocol, store, [task])
    assert [arm for arm, _ in requests] == ["common", "B", "C", "D"]
    assert json.loads(success.read_text()) == a_row
    assert {row["state"] for row in result["rows"]} == {
        "COMPLETE", "INTERRUPTED" if failure == "process_exit" else "ERROR",
    }
    assert result["statistics"]["complete"] and not result["completed"]
    assert open_calls(store) == pending
    with pytest.raises(ValueError, match="without evidence"):
        audit_historical_reservations(store, [])
    audit = audit_historical_reservations(store, [root])
    assert len(audit["calls"]) == 1 and audit["calls"][0]["run_id"] == requests[1][1]
    success.unlink()
    invalid = comparison.resume_test_matrix(options, root, protocol, store, [task])
    assert "no matching test intent" in invalid["error"]
    assert len(requests) == 4
