import importlib.util
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest


def load_driver():
    path = Path(__file__).resolve().parents[1] / "scripts/continue_route_study.py"
    spec = importlib.util.spec_from_file_location("route_continuation", path)
    driver = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(driver)
    return driver


driver = load_driver()


def test_observer_preserves_request_and_records_only_limited_429_metadata():
    received = []

    class Server(BaseHTTPRequestHandler):
        def do_POST(self):
            received.append((self.path, self.headers["Authorization"], self.rfile.read(int(self.headers["Content-Length"]))))
            self.send_response(429)
            self.send_header("Retry-After", "90")
            self.end_headers()
            self.wfile.write(b'{"error":{"code":"1305","message":"unexported"}}')

        def log_message(self, *args):
            pass

    with HTTPServer(("127.0.0.1", 0), Server) as local:
        thread = threading.Thread(target=local.handle_request, daemon=True)
        thread.start()
        transport = driver.ObserveRateLimit()
        with httpx.Client(transport=transport) as client:
            result = client.post(f"http://127.0.0.1:{local.server_port}/example?a=b",
                                 content=b'{"k":1}', headers={"Authorization": "test-secret"})
        thread.join(timeout=5)
    assert received == [("/example?a=b", "test-secret", b'{"k":1}')]
    assert result.status_code == 429 and result.json()["error"]["code"] == "1305"
    assert set(transport.last) == {"observed_at", "status", "code", "retry_after_seconds"}
    assert transport.last["retry_after_seconds"] == 90 and transport.last["code"] == 1305
    assert driver.after_seconds("Thu, 01 Jan 1970 00:02:03 GMT", 5) == 118
    assert driver.after_seconds("bad header", 0) is None


@pytest.mark.parametrize("code,continues", [(1302, True), (1305, True), (1308, False), (1313, False), (None, False)])
def test_error_classification_and_reuse(tmp_path, monkeypatch, code, continues):
    states = {"parent": "EXHAUSTED", "first": "ERROR", "second": "ERROR", "done": "FAILED"}
    calls = []
    observation = SimpleNamespace(last=None)

    class Runner:
        def solve(self, task, **kwargs):
            calls.append(task.id)
            observation.last = {"status": 429, "code": code, "retry_after_seconds": 90, "observed_at": 1}
            return "second" if len(calls) == 1 else "done"

    store = SimpleNamespace(run=lambda key: {"created": 1, "result": {"seconds": 2}})
    monkeypatch.setattr(driver, "check_binding", lambda store, rid, *args: states[rid])
    monkeypatch.setattr(driver, "rate_receipt", lambda store, rid: {"run_id": rid})
    recovery = driver.ContinueArms(Runner(), observation, store, {}, {"run_ids": ["parent", "first"]}, tmp_path)
    task = SimpleNamespace(id="A")
    assert recovery.solve(task, skill_ids=["parent-skill"]) == "parent"
    assert calls == []
    if continues:
        assert recovery.solve(task, skill_ids=["child-skill"]) == "done"
        assert len(calls) == 2
    else:
        with pytest.raises(RuntimeError, match="requires a stop"):
            recovery.solve(task, skill_ids=["child-skill"])
        assert len(calls) == 1
    retries = json.loads((tmp_path / "retry-events.json").read_text())
    assert [r["run_id"] for r in retries] == ["first", "second"]
    assert retries[1]["provider"]["code"] == code
    assert recovery.reused == ["parent"]
