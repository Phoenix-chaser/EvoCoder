"""Offline accounting and membership checks for interrupted routing pairs."""
import importlib.util
import json
import sqlite3
from pathlib import Path

import pytest


def auditor():
    file = Path(__file__).resolve().parents[1] / "scripts/verify_route_study.py"
    spec = importlib.util.spec_from_file_location("verify_route_study", file)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def resumed(tmp_path):
    (tmp_path/"traces").mkdir()
    db, bills = sqlite3.connect(":memory:"), sqlite3.connect(":memory:")
    db.execute("CREATE TABLE runs (id,state,result,config,task_id,task_hash,skill_id,split,purpose,mode)")
    db.execute("CREATE TABLE events (seq,kind,payload,run_id)")
    bills.execute("CREATE TABLE calls (id,state,actual,reserved,run_id)")
    rate = "RuntimeError: model_http_429: reservation retained"
    settings = {"model":"fixture"}
    for key, skill, state, spent in (("retained","p","EXHAUSTED",3),("error","c","ERROR",1),("retry","c","COMPLETE",5)):
        result = {"error": rate if state == "ERROR" else ""}
        run = dict(id=key,state=state,result=result,config=settings,task_id="t",task_hash="h",skill_id=skill,split="dev")
        db.execute("INSERT INTO runs VALUES(?,?,?,?,?,?,?,?,?,?)",
                   (key,state,json.dumps(result),json.dumps(settings),"t","h",skill,"dev","evaluate","live"))
        raw_events = [("START",{"requested_skill_ids":[skill]}), ("MODEL_RESPONSE",{"call_id":key})]
        if state == "ERROR":
            raw_events.append(("ERROR", {"message":rate}))
        events = []
        for n,(kind,v) in enumerate(raw_events,1):
            events.append({"seq":n,"kind":kind,"payload":v})
            db.execute("INSERT INTO events VALUES(?,?,?,?)",(n,kind,json.dumps(v),key))
        (tmp_path/"traces"/(key+".json")).write_text(json.dumps({"run":run,"events":events}))
        bills.execute("INSERT INTO calls VALUES(?,?,?,?,?)",(key,"settled",spent*10**9,20*10**9,key))
    bills.execute("INSERT INTO calls VALUES(?,?,?,?,?)",("lost","reserved",None,2*10**9,"error"))
    pending = [{"id":"lost","run_id":"error","reserved":2*10**9}]
    source = {"run_ids":["retained","error"],"rows":[]}
    binding = {"budget_at_resume":{"spent_credits":14,"reserved_credits":2}, "pending_at_resume":pending}
    protocol = {"settings":settings, "budget_before":{"spent_credits":10,"reserved_credits":0}}
    result = {"run_ids":["retained","error","retry"],"reused_terminal_runs":["retained"],"rows":[],
              "completed":True, "budget_before":protocol["budget_before"],
              "budget_after":{"spent_credits":19,"reserved_credits":2}, "new_cost_credits":9,
              "run_costs":{"calls":4,"cost_credits":9,"reserved_credits":2},
              "continuation_costs":{"calls":1,"cost_credits":5,"reserved_credits":0},
              "pending_after":pending,
              "failed_attempts":[{"run_id":"error","pending_call_id":"lost","pending_nanos":2*10**9,
                                  "settled_nanos":10**9,"call_ids":["error","lost"],"task_id":"t",
                                  "policy":"normal","skill_id":"c","provider":None}]}
    yield db,bills,tmp_path,protocol,result,source,binding,["retained","retry"]
    db.close()
    bills.close()


def test_resume_scores_retained_terminal_once_and_accounts_for_failed_prefix(resumed):
    summary = auditor().check_attempts(*resumed)
    assert summary["scored_run_count"] == 2
    assert summary["error_run_ids"] == ["error"]
    assert summary["study_reserved_credits"] == "2"


@pytest.mark.parametrize("invalid", ["score_error","omit_failure","reuse_twice","omit_orphan","ignore_paid_prefix",
                                     "charge_old_again","release_reservation","omit_call_id","wrong_owner"])
def test_invalid_resume_records_rejected(resumed, invalid):
    db,bills,out,p,r,old,binding,scored = resumed
    if invalid == "score_error":
        scored.append("error")
    elif invalid == "omit_failure":
        r["failed_attempts"] = []
    elif invalid == "reuse_twice":
        r["reused_terminal_runs"].append("retained")
    elif invalid == "omit_orphan":
        scored.remove("retained")
    elif invalid == "ignore_paid_prefix":
        r["run_costs"]["cost_credits"] = 8
    elif invalid == "charge_old_again":
        r["continuation_costs"]["cost_credits"] = 9
    elif invalid == "release_reservation":
        r["budget_after"]["reserved_credits"] = 0
    elif invalid == "omit_call_id":
        r["failed_attempts"][0]["call_ids"] = ["lost"]
    else:
        binding["pending_at_resume"][0]["run_id"] = "other"
    with pytest.raises(AssertionError):
        auditor().check_attempts(*resumed)
