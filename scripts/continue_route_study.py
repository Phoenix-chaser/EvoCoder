"""Continue a stopped Click route matrix after a confirmed HTTP 429, in a new archive."""

import argparse
import fcntl
import json
import math
import re
import shutil
import time
from collections import deque
from datetime import timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

import httpx

from evocoder.comparison import dump, frozen_knowledge, sources
from evocoder.contracts import Settings, digest
from evocoder.dataset import load_catalog
from evocoder.library import SkillLibrary
from evocoder.model import Model
from evocoder.revision_diagnostic import check_fresh_holdouts, copy_closed_seed, read
from evocoder.route_analysis import analyze_routes
from evocoder.route_study import pair, remaining_gates
from evocoder.runtime import Runner
from evocoder.store import Store

BACKOFF = (60, 180, 600)
MAX_RETRIES_TOTAL = 6
MAX_WAIT = 21600
RATE_ERROR = "RuntimeError: model_http_429: reservation retained"


def after_seconds(value, now):
    if not value or len(value) > 100:
        return None
    if re.fullmatch(r"[0-9]{1,12}", value):
        return int(value)
    try:
        date = parsedate_to_datetime(value)
        if date.tzinfo is None:
            date = date.replace(tzinfo=timezone.utc)
        return max(0, math.ceil(date.timestamp() - now))
    except (TypeError, ValueError, OverflowError):
        return None


class ObserveRateLimit(httpx.BaseTransport):
    """Forward the built request once with HTTPX's usual TLS, proxy and timeout handling."""

    def __init__(self):
        self.last = None

    def handle_request(self, request):
        with httpx.Client(follow_redirects=False) as client:
            response = client.send(request)
        if response.status_code == 429:
            now = time.time()
            try:
                payload = response.json()
            except ValueError:
                payload = None
            error = payload.get("error") if isinstance(payload, dict) else None
            code = str(error.get("code", "")) if isinstance(error, dict) else ""
            self.last = {"status": 429, "code": int(code) if re.fullmatch(r"\d{1,8}", code) else None,
                         "retry_after_seconds": after_seconds(response.headers.get("Retry-After"), now),
                         "observed_at": now}
        return response


def run_ids(store, last):
    with store.connect() as db:
        return [r[0] for r in db.execute("SELECT id FROM runs WHERE rowid>? ORDER BY rowid", (last,))]


def pending_calls(ledger):
    with ledger.connect() as db:
        return [dict(row) for row in db.execute(
            "SELECT id,run_id,reserved FROM calls WHERE state='reserved' ORDER BY id")]


def rate_receipt(store, rid):
    run = store.run(rid)
    if run["state"] != "ERROR" or run["result"].get("error") != RATE_ERROR:
        raise ValueError(f"non-retryable outcome in {rid}")
    events = store.events(rid)
    if not any(e["kind"] == "ERROR" and e["payload"].get("message") == RATE_ERROR for e in events):
        raise ValueError(f"no recorded provider HTTP 429 in {rid}")
    with store.ledger.connect() as db:
        bills = [dict(row) for row in db.execute(
            "SELECT id,state,reserved,actual FROM calls WHERE run_id=? ORDER BY rowid", (rid,))]
    unresolved = [b for b in bills if b["state"] == "reserved"]
    charged = {b["id"] for b in bills if b["state"] == "settled"}
    responses = {e["payload"]["call_id"] for e in events if e["kind"] == "MODEL_RESPONSE"}
    if (not bills or len(unresolved) != 1 or bills[-1] != unresolved[0]
            or responses != charged or len(bills) != len(charged) + 1):
        raise ValueError(f"HTTP 429 reservation disagrees with the recorded calls in {rid}")
    return {"run_id": rid, "call_ids": [b["id"] for b in bills], "settled_nanos": sum(b["actual"] for b in bills if b["state"] == "settled"),
            "pending_call_id": unresolved[0]["id"], "pending_nanos": unresolved[0]["reserved"]}


def check_binding(store, rid, task, skill, fixed, protocol):
    run = store.run(rid)
    start, = [e["payload"] for e in store.events(rid) if e["kind"] == "START"]
    if (run["task_hash"] != digest(task.model_dump()) or run["task_id"] != task.id
        or run["split"] != task.split or run["config"] != protocol["settings"]
        or run["purpose"] != "evaluate" or run["mode"] != "live"
        or start["requested_skill_ids"] != [skill] or start.get("fixed_route") != fixed
        or run["result"]["image_id"] != protocol["settings"]["image"]):
        raise ValueError(f"arm differs from the frozen task/policy/version in {rid}")
    if run["state"] not in {"COMPLETE", "FAILED", "EXHAUSTED", "ERROR"}:
        raise ValueError(f"unresolved arm {rid}")
    return run["state"]


class ContinueArms:
    def __init__(self, runner, transport, store, protocol, old, output):
        self.runner, self.transport, self.store = runner, transport, store
        self.protocol, self.old, self.output = protocol, deque(old["run_ids"]), output
        self.recorded_errors = {x["run_id"]: x for x in old.get("failed_attempts", [])}
        self.failures = []
        self.reused = []

    def solve(self, task, **kwargs):
        skill, = kwargs["skill_ids"]
        fixed = kwargs.get("fixed_route")
        attempts = 0
        while True:
            self.transport.last = None
            replay = bool(self.old)
            rid = self.old.popleft() if replay else self.runner.solve(task, **kwargs)
            state = check_binding(self.store, rid, task, skill, fixed, self.protocol)
            if state != "ERROR":
                if replay:
                    self.reused.append(rid)
                return rid
            info = rate_receipt(self.store, rid)
            observed = self.recorded_errors.get(rid, {}).get("provider") if replay else self.transport.last
            if not replay and observed is None:
                raise ValueError("missing transport-side observation of HTTP 429")
            attempts += 1
            ended = self.store.run(rid)["created"] + self.store.run(rid)["result"]["seconds"]
            delay = BACKOFF[min(attempts, len(BACKOFF)) - 1]
            if observed:
                delay = max(delay, observed.get("retry_after_seconds") or 0)
                ended = observed["observed_at"]
            info.update(task_id=task.id, skill_id=skill, policy=f"fixed_{fixed}" if fixed else "normal",
                        provider=observed, retry_at=ended + delay, attempt_in_slot=attempts)
            self.failures.append(info)
            dump(self.output / "retry-events.json", self.failures)
            if self.old:
                continue  # Historical attempts are already billed. Replay their terminal outcomes.
            if (observed is not None and observed.get("code") not in {1302, 1305}) or (
                attempts > len(BACKOFF) or len(self.failures) > MAX_RETRIES_TOTAL or delay > MAX_WAIT
            ):
                raise RuntimeError(f"confirmed 429 requires a stop at {rid}; see retry-events.json")
            while (remaining := info["retry_at"] - time.time()) > 0:
                time.sleep(min(remaining, 30))


def execute(settings, previous, output):
    previous, output = previous.resolve(), output.resolve()
    p, old, started = (read(previous / name) for name in ("protocol.json", "result.json", "started.json"))
    opts = settings.model_copy(update={"image": p["settings"]["image"]})
    opts.validate_live()
    if (old["completed"] or not old["run_ids"] or old["protocol"] != p or p["claim"] != "routing_intervention_diagnostic"
            or old["phase"] not in {"routing_matrix", "full_normal_gate"} or old["holdout"]["rows"]
            or digest(p) != started["protocol_hash"] or opts.public() != p["settings"]
            or digest(sources()) != p["source_hash"]
            or digest({f.name: f.read_text() for f in (previous/"source").glob("*.py")}) != p["source_hash"]
            or digest(read(previous/"historical-routes.json")) != p["audit_hash"]
            or digest(read(Path(p["origin"])/"result.json")) != p["origin_result_hash"]
            or digest(read(Path(p["manifest"]))) != p["manifest_hash"]):
        raise ValueError("source experiment or configuration is not an intact interrupted route study")
    ledger = Store(opts.state_dir, unit=opts.accounting_unit)
    tasks = {t.id: t for t in load_catalog(Path(p["manifest"]))}
    last = started["previous_last_row"]
    with (ledger.root/"comparison.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if ledger.budget() != old["budget_after"] or ledger.budget()["blocked"]:
            raise ValueError("shared billing state changed since interruption")
        check_fresh_holdouts([tasks[k] for k in p["test_tasks"]], Path(p["origin"]).parent, ledger)
        store = copy_closed_seed(previous, output, ledger)
        if run_ids(store, last) != old["run_ids"] or frozen_knowledge(store) != p["knowledge_hash"]:
            raise ValueError("recorded source runs or skills changed")
        rate_receipt(store, old["run_ids"][-1])
        if store.costs(old["run_ids"]) != old["run_costs"]:
            raise ValueError("recorded origin costs changed")
        shutil.copytree(previous / "source", output / "source")
        for name in ("protocol.json", "historical-routes.json", "started.json"):
            shutil.copy2(previous / name, output / name)
        shutil.copy2(Path(__file__), output / "continuation-driver.py")
        bindings = {"previous": str(previous), "result_hash": digest(old), "protocol_hash": digest(p),
                    "driver_hash": digest(Path(__file__).read_text()), "retained_run_ids": old["run_ids"],
                    "budget_at_resume": ledger.budget(), "pending_at_resume": pending_calls(ledger),
                    "backoff_seconds": BACKOFF, "max_retries_total": MAX_RETRIES_TOTAL}
        dump(output / "continuation.json", bindings)
        transport = ObserveRateLimit()
        runner = Runner(opts, store, Model(opts, store, transport))
        recovery = ContinueArms(runner, transport, store, p, old, output)
        library = SkillLibrary(opts, store, recovery)
        result = {"protocol": p, "completed": False, "phase": "routing_matrix", "rows": [],
                  "holdout": {"status": "pending_gate", "rows": []}, "full_gate": {"status": "pending", "rows": []},
                  "budget_before": p["budget_before"], "continuation_hash": digest(bindings)}
        try:
            for index, cell in enumerate(p["schedule"]):
                row = pair(library, tasks[cell["task_id"]], cell["repeat"], cell["policy"], cell["reverse"], p)
                if index < len(old["rows"]) and row != old["rows"][index]:
                    raise ValueError("completed source pair changed during replay")
                result["rows"].append(row)
                dump(output / "progress.json", result)
            result["analysis"] = analyze_routes(result["rows"], tasks=p["tasks"], parent=p["parent"],
                                                child=p["child"], repeats=p["repeats"])
            if result["analysis"]["gate"]["passed"]:
                remaining_gates(library, tasks, p, result, output)
            else:
                result["holdout"]["status"] = result["full_gate"]["status"] = "blocked_by_route_gate"
            if recovery.old:
                raise ValueError("unconsumed runs from the source archive")
            result.update(completed=True, phase="complete")
        except (KeyboardInterrupt, Exception) as exc:
            result["error"] = (type(exc).__name__ + ": " + str(exc)).replace(opts.api_key, "[redacted]")[:1500]
        finally:
            if not result["completed"] and result["holdout"]["status"] in {"pending_gate", "running"}:
                result["holdout"]["status"] = "stopped" if result["holdout"]["rows"] else "blocked_incomplete"
            ids = run_ids(store, last)
            result.update(run_ids=ids, reused_terminal_runs=recovery.reused, failed_attempts=recovery.failures,
                          run_costs=store.costs(ids), budget_after=ledger.budget(), pending_after=pending_calls(ledger),
                          continuation_costs=store.costs([key for key in ids if key not in old["run_ids"]]))
            result["new_cost_credits"] = round(result["budget_after"]["spent_credits"] - p["budget_before"]["spent_credits"], 8)
            delta = round(result["budget_after"]["spent_credits"] - old["budget_after"]["spent_credits"], 8)
            if (abs(result["new_cost_credits"] - result["run_costs"]["cost_credits"]) > 0.000001
                    or abs(delta - result["continuation_costs"]["cost_credits"]) > 0.000001):
                result.update(completed=False, error="shared ledger disagrees with study/continuation totals")
            dump(output / "result.json", result)
        return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--previous", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    outcome = execute(Settings.from_env_file(args.env_file), args.previous, args.output)
    print(json.dumps({k: outcome.get(k) for k in ("completed", "phase", "error", "new_cost_credits", "continuation_costs")}))
    raise SystemExit(0 if outcome["completed"] else 1)
