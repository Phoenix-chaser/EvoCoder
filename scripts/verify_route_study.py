"""Verify the sealed route-study export and cross-policy solver request hashes; no model calls."""
import argparse
import json
import sqlite3
from collections import Counter, defaultdict
from contextlib import closing
from decimal import Decimal
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlsplit, unquote

from evocoder.contracts import digest
from evocoder.gate import gate_summary
from evocoder.route_analysis import analyze_routes


def read(path):
    return json.loads(path.read_text())


def readonly(path):
    wal = path.with_name(path.name + "-wal")
    mode = "?mode=ro" if wal.exists() and wal.stat().st_size else "?mode=ro&immutable=1"
    return closing(sqlite3.connect(path.resolve().as_uri() + mode, uri=True))


UNIT = Decimal(10**9)


def nanos(value):
    scaled = Decimal(str(value)) * UNIT
    assert scaled == scaled.to_integral()
    return int(scaled)


def check_chain(root, out, p, r):
    """Verify the driver and result chain back to the first interrupted archive."""
    previous, history, visited = {}, [], {root.resolve()}
    folder, current = root, r
    while (folder / "continuation.json").is_file():
        binding = read(folder / "continuation.json")
        assert digest(binding) == current["continuation_hash"]
        prior = Path(binding["previous"]).resolve()
        assert prior not in visited
        visited.add(prior)
        old = read(prior / "result.json")
        assert not old["completed"] and binding["result_hash"] == digest(old)
        assert old["protocol"] == p == read(prior / "protocol.json")
        assert binding["protocol_hash"] == digest(p)
        assert (folder / "protocol.json").read_bytes() == (prior / "protocol.json").read_bytes()
        assert (folder / "started.json").read_bytes() == (prior / "started.json").read_bytes()
        assert read(prior / "historical-routes.json") == read(folder / "historical-routes.json")
        assert digest({f.name:f.read_text() for f in (prior/"source").glob("*.py")}) == p["source_hash"]
        assert digest((folder / "continuation-driver.py").read_text()) == binding["driver_hash"]
        old_ids = old["run_ids"]
        assert binding["retained_run_ids"] == old_ids == current["run_ids"][:len(old_ids)]
        assert old["rows"] == current["rows"][:len(old["rows"])]
        assert binding["budget_at_resume"] == old["budget_after"]
        assert sum(item["reserved"] for item in binding["pending_at_resume"]) == nanos(old["budget_after"]["reserved_credits"])
        if folder == root:
            previous = old
            assert read(out/"interrupted-source-result.json") == old
            for name in ("continuation.json", "continuation-driver.py", "retry-events.json"):
                assert (root/name).is_file() == (out/name).is_file()
                if (root/name).is_file():
                    assert (root/name).read_bytes() == (out/name).read_bytes()
            assert not current.get("failed_attempts") or read(folder/"retry-events.json") == current["failed_attempts"]
        with readonly(prior/"seed/evocoder.sqlite3") as earlier, readonly(folder/"seed/evocoder.sqlite3") as copied:
            for key in old_ids:
                for query in ("SELECT * FROM runs WHERE id=?",
                              "SELECT seq,kind,payload FROM events WHERE run_id=? ORDER BY seq"):
                    rows = earlier.execute(query,(key,)).fetchall()
                    assert rows and rows == copied.execute(query,(key,)).fetchall()
        history.append({"source": str(prior), "retained": len(old_ids), "source_result": binding["result_hash"]})
        folder, current = prior, old
    assert "continuation_hash" not in current and folder.is_dir()
    return previous, history


def check_attempts(db, bills, out, p, r, previous, binding, scored):
    """Reconcile all calls including paid prefixes of failed and unpaired arms."""
    ids = r["run_ids"]
    assert len(ids) == len(set(ids)) and len(scored) == len(set(scored))
    prior = set(previous.get("run_ids", []))
    recorded = {e["run_id"]: e for e in r.get("failed_attempts", [])}
    assert len(recorded) == len(r.get("failed_attempts", []))
    totals, new = Counter(), Counter()
    terminal, errors, unresolved = [], [], []
    pending_new = []
    for key in ids:
        entry = read(out/"traces"/(key+".json"))
        run = entry["run"]
        row = db.execute("SELECT id,state,result,config,task_id,task_hash,skill_id,split,purpose,mode FROM runs WHERE id=?", (key,)).fetchone()
        assert row and (row[0], row[1], *row[4:]) == (key, run["state"], run["task_id"], run["task_hash"],
                                                       run["skill_id"], run["split"], "evaluate", "live")
        assert run["id"] == key and run["result"] == json.loads(row[2]) and run["config"] == json.loads(row[3]) == p["settings"]
        events = [{"seq":n, "kind":k, "payload":json.loads(raw)} for n,k,raw in db.execute(
            "SELECT seq,kind,payload FROM events WHERE run_id=? ORDER BY seq",(key,))]
        assert entry["events"] == events
        calls = list(bills.execute("SELECT id,state,actual,reserved FROM calls WHERE run_id=? ORDER BY rowid", (key,)))
        settled = {c[0] for c in calls if c[1]=="settled"}
        waiting = [c for c in calls if c[1]=="reserved"]
        responses = [e["payload"]["call_id"] for e in events if e["kind"]=="MODEL_RESPONSE"]
        assert len(settled) + len(waiting) == len(calls) and all(c[2] is None for c in waiting)
        assert set(responses) <= settled and len(responses)==len(set(responses))
        spent = sum(c[2] for c in calls if c[1]=="settled")
        reserved = sum(c[3] for c in waiting)
        counts = {"cost_credits": spent, "reserved_credits": reserved, "calls": len(calls)}
        totals.update(counts)
        if key not in prior:
            new.update(counts)
            pending_new.extend({"id":c[0], "run_id":key, "reserved":c[3]} for c in waiting)
        if waiting:
            unresolved.append(key)
        if run["state"] in {"COMPLETE", "FAILED", "EXHAUSTED"}:
            terminal.append(key)
            assert not waiting and len(responses)==len(calls) and key not in recorded
        else:
            errors.append(key)
            assert key not in scored
        if key in recorded:
            error = recorded[key]
            assert run["state"] == "ERROR" and run["result"]["error"] == "RuntimeError: model_http_429: reservation retained"
            assert error["call_ids"] == [c[0] for c in calls]
            assert error["settled_nanos"] == spent and len(waiting) == 1 and waiting[0] == calls[-1]
            assert error["pending_call_id"] == waiting[0][0] and error["pending_nanos"] == reserved
            assert len(responses) == len(settled)
            assert any(e["kind"]=="ERROR" and e["payload"]["message"] == run["result"]["error"] for e in events)
            start, = [e["payload"] for e in events if e["kind"]=="START"]
            override = start.get("fixed_route")
            assert error["policy"] == ("fixed_"+override if override else "normal")
            assert error["task_id"] == run["task_id"] and start["requested_skill_ids"] == [error["skill_id"]]
            if key not in prior or error["provider"] is not None:
                assert error["provider"]["status"] == 429
    assert set(recorded) <= set(errors)
    assert set(scored) <= set(terminal)
    if r["completed"]:
        assert set(scored) == set(terminal) and set(errors) == set(recorded)
    def check_costs(summary, observed):
        assert summary["calls"] == observed["calls"]
        for field in ("cost_credits", "reserved_credits"):
            assert nanos(summary[field]) == observed[field]
    check_costs(r["run_costs"], totals)
    before, after = p["budget_before"], r["budget_after"]
    assert before == r["budget_before"]
    assert nanos(r["new_cost_credits"]) == totals["cost_credits"] == nanos(after["spent_credits"])-nanos(before["spent_credits"])
    assert totals["reserved_credits"] == nanos(after["reserved_credits"])-nanos(before["reserved_credits"])
    if previous:
        reused = r["reused_terminal_runs"]
        expected = [i for i in terminal if i in prior]
        assert reused == expected[:len(reused)]
        if r["completed"] or any(key not in prior for key in ids):
            assert reused == expected
        assert r["rows"][:len(previous["rows"])] == previous["rows"]
        check_costs(r["continuation_costs"], new)
        resume = binding["budget_at_resume"]
        assert new["cost_credits"] == nanos(after["spent_credits"])-nanos(resume["spent_credits"])
        assert new["reserved_credits"] == nanos(after["reserved_credits"])-nanos(resume["reserved_credits"])
        pending = sorted(binding["pending_at_resume"]+pending_new, key=lambda e:e["id"])
        assert len(pending) == len({x["id"] for x in pending}) and pending == r["pending_after"]
        assert sum(c["reserved"] for c in pending)==nanos(after["reserved_credits"])
        for item in pending:
            assert bills.execute("SELECT run_id,reserved FROM calls WHERE id=?",(item["id"],)).fetchone() == (
                item["run_id"], item["reserved"])
    return {"all_run_count":len(ids), "scored_run_count":len(scored), "error_run_ids":errors,
            "pending_run_ids":unresolved, "unpaired_terminal_ids":[i for i in terminal if i not in scored],
            "reused_terminal_runs":r.get("reused_terminal_runs", []),
            "calls":totals["calls"], "study_reserved_credits":str(Decimal(totals["reserved_credits"])/UNIT),
            "continuation_costs":r.get("continuation_costs")}


class Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.paths = []

    def handle_starttag(self, tag, attrs):
        href = dict(attrs).get("href")
        if tag == "a" and href and not urlsplit(href).scheme:
            path = unquote(urlsplit(href).path)
            if path:
                self.paths.append(path)


def verify(root, out):
    p, r, archive = read(root/"protocol.json"), read(root/"result.json"), root/"seed/evocoder.sqlite3"
    assert p == read(out/"protocol.json") == r["protocol"] and r == read(out/"result.json")
    assert digest(p) == read(root/"started.json")["protocol_hash"]
    assert digest(read(out/"historical-routes.json")) == p["audit_hash"]
    assert digest(read(Path(p["origin"])/"result.json")) == p["origin_result_hash"]
    assert digest({s.name: s.read_text() for s in (root/"source").glob("*.py")}) == p["source_hash"]
    assert digest(read(Path(p["manifest"]))) == p["manifest_hash"]
    previous, history = check_chain(root, out, p, r)
    for idx, row in enumerate(r["rows"]):
        scheduled = p["schedule"][idx]
        assert all(scheduled[k] == row[k] for k in ("task_id", "policy", "repeat"))
        assert row["arm_order"] == (["candidate", "baseline"] if scheduled["reverse"] else ["baseline", "candidate"])
    a = analyze_routes(r["rows"], tasks=p["tasks"], parent=p["parent"], child=p["child"], repeats=p["repeats"])
    if r.get("analysis") is not None:
        assert a == r["analysis"]
    full, held = r["full_gate"], r["holdout"]
    if not a["gate"]["passed"]:
        assert not full["rows"] and not held["rows"]
    if full["rows"]:
        assert [x for x in full["rows"] if x["task_id"] in p["tasks"]] == [
            x for x in r["rows"] if x["policy"] == "normal"]
    if full.get("gate"):
        for key, value in gate_summary(full["rows"], p["repeats"]).items():
            assert full[key] == value
    if held["rows"]:
        assert full["gate"]["passed"]
    if r["completed"]:
        assert len(r["rows"]) == len(p["schedule"])
        if a["gate"]["passed"]:
            assert full["status"] == "complete"
            expected = Counter({k:p["repeats"] for k in p["prior_dev_tasks"]+p["regression_tasks"]})
            expected.update(p["support_tasks"])
            assert Counter(row["task_id"] for row in full["rows"]) == expected
            if full["gate"]["passed"]:
                assert held["status"] == "complete"
                assert Counter(row["task_id"] for row in held["rows"]) == Counter({t:p["holdout_repeats"] for t in p["test_tasks"]})
            else:
                assert held["status"] == "blocked_by_full_gate"
        else:
            assert full["status"] == held["status"] == "blocked_by_route_gate"
    pairs = [*r["rows"], *(x for x in full["rows"] if x["task_id"] not in p["tasks"]), *held["rows"]]
    ids = [row[arm + "_run"] for row in pairs for arm in ("baseline", "candidate")]
    assert len(ids) == len(set(ids)) and set(ids) <= set(r["run_ids"])
    # With the same task, version (or empty skill set), and first action budget, every
    # policy should generate exactly the same first solver request body.
    first = defaultdict(set)
    routing = defaultdict(set)
    costs = Counter()
    ledger = Path(p["settings"]["state_dir"]) / "evocoder.sqlite3"
    with readonly(archive) as db, readonly(ledger) as bills:
        start = read(root/"started.json")["previous_last_row"]
        assert [x[0] for x in db.execute("SELECT id FROM runs WHERE rowid>? ORDER BY rowid",(start,))] == r["run_ids"]
        attempts = check_attempts(db, bills, out, p, r, previous,
                                  read(root/"continuation.json") if previous else None, ids)
        for row in pairs:
            for arm, skill in (("baseline", p["parent"]), ("candidate", p["child"])):
                key = row[arm + "_run"]
                exported = read(out/"traces"/(key+".json"))
                task, task_hash, applied, raw = db.execute(
                    "SELECT task_id,task_hash,skill_id,result FROM runs WHERE id=?", (key,)).fetchone()
                run, result = exported["run"], json.loads(raw)
                assert task == row["task_id"] == run["task_id"] and task_hash == row["task_hash"]
                assert result == run["result"] and run["config"] == p["settings"]
                assert result["passed"] == row[arm+"_pass"] and result["image_id"] == p["settings"]["image"]
                assert applied == row[arm+"_applied_skill"] == run["skill_id"]
                assert applied == (skill if row[arm+"_route"] == "apply" else None)
                events = [{"seq":s,"kind":k,"payload":json.loads(v)} for s,k,v in db.execute(
                    "SELECT seq,kind,payload FROM events WHERE run_id=? ORDER BY seq",(key,))]
                assert exported["events"] == events
                model = [ev["payload"] for ev in events if ev["kind"]=="MODEL_RESPONSE"]
                solver = [z for z in model if z["expected_function"]=="submit_action"]
                router = [z for z in model if z["expected_function"]=="select_library_skill"]
                assert len(model)==run["result"]["model_steps"]==row[arm+"_model_steps"]
                assert solver and len(router)==int(row["policy"]=="normal")
                fixed = row["policy"]!="normal"
                assert row[arm+"_budget_steps"]==len(model)+int(fixed)<=p["settings"]["max_steps"]
                first[task,applied].add(solver[0]["request_body_hash"])
                assert solver[0]["request_body_hash"]==row[arm+"_first_solver_request_hash"]
                if router and task in p["tasks"]:
                    routing[task,arm].add(router[0]["request_body_hash"])
                    assert router[0]["request_body_hash"]==p["historical_route_bodies"][task][arm]
                if fixed:
                    memory = [] if not applied else [json.loads(db.execute(
                        "SELECT body FROM artifacts WHERE id=?", (applied,)).fetchone()[0])]
                    assert digest({"skills":memory})==row[arm+"_solver_memory_hash"]
                actual = list(bills.execute("SELECT id,state,actual FROM calls WHERE run_id=?",(key,)))
                assert {c[0] for c in actual}=={z["call_id"] for z in model}
                assert all(c[1]=="settled" for c in actual)
                spent = Decimal(sum(c[2] for c in actual)) / Decimal(10**9)
                assert spent==Decimal(str(row[arm+"_credits"]))
                costs[row["policy"],arm]+=spent
        check_ids = tuple(p["test_tasks"])
        slots = ",".join("?" for _ in check_ids)
        if not full.get("gate", {}).get("passed"):
            assert not db.execute(f"SELECT 1 FROM runs WHERE task_id IN ({slots})",check_ids).fetchone()
            assert not bills.execute(f"SELECT 1 FROM test_exposures WHERE task_id IN ({slots})",check_ids).fetchone()
    assert all(len(hashes)==1 for hashes in first.values())
    assert all(len(hashes)==1 for hashes in routing.values())
    links=Links()
    links.feed((out/"report.html").read_text())
    assert all((out/link).resolve().is_relative_to(out.resolve()) and (out/link).is_file() for link in links.paths)
    trace_paths = {link for link in links.paths if link.startswith("traces/")}
    assert {f"traces/{key}.json" for key in ids} <= trace_paths <= {f"traces/{key}.json" for key in r["run_ids"]}
    if previous:
        assert {"continuation.json", "continuation-driver.py"} <= set(links.paths)
        assert {f"traces/{key}.json" for key in attempts["error_run_ids"]} <= trace_paths
    if (root/"patch-probes.json").is_file():
        assert "patch-probes.json" in links.paths
        assert (out/"patch-probes.json").read_bytes() == (root/"patch-probes.json").read_bytes()
        assert read(out/"patch-probes.json")["result_hash"] == digest(r)
    return {"completed":r["completed"],"run_count":len(ids),"unique_solver_request_by_task_and_memory":True,
            "historical_normal_body_hash_verified_for_arms":len(routing),
            "known_memory_cases_checked":len(first),"diagnostic_complete":a["gate"]["complete"],
            "original_full_gate_status":full["status"],"holdout_status":held["status"],
            "checked_trace_links":len(trace_paths),"new_cost_credits":r["new_cost_credits"],
            "continuation_chain":history, "attempts":attempts,
            "costs_per_policy_and_arm":{"/".join(k):str(v) for k,v in sorted(costs.items())}}


if __name__ == "__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root",type=Path)
    parser.add_argument("output",type=Path)
    args=parser.parse_args()
    print(json.dumps(verify(args.root,args.output),ensure_ascii=False,indent=2))
