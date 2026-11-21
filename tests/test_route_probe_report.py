import importlib.util
import json
import sqlite3
from pathlib import Path

import pytest

from evocoder.contracts import digest


COUNT = "pallets-click-3d029e03eb"
HINT = "pallets-click-42e2821ba3"


def reporter():
    path = Path(__file__).resolve().parents[1] / "scripts/report_route_study.py"
    spec = importlib.util.spec_from_file_location("route_probe_report", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def data(tmp_path):
    protocol = {"tasks":{COUNT:"c",HINT:"h"},"settings":{"image":"img"}}
    r = {"protocol":protocol,"phase":"routing_matrix","completed":False,"rows":[],"run_ids":["terminal","lost"]}
    (tmp_path/"seed").mkdir()
    con = sqlite3.connect(tmp_path/"seed/evocoder.sqlite3")
    con.execute("CREATE TABLE runs (id,task_id,task_hash,state,skill_id,result,split,purpose)")
    trials = []
    for key, task, state, patch, passed in (("terminal",COUNT,"FAILED","changed",False),("lost",HINT,"ERROR",None,False)):
        con.execute("INSERT INTO runs VALUES(?,?,?,?,?,?,?,?)",(
            key,task,task+"hash",state,None,json.dumps({"patch":patch,"passed":passed}),"dev","evaluate"))
        trials.append({"run_id":key,"task_id":task,"state":state,"patch_hash":patch,"passed":passed,
                       "applied_skill":None,"actual_route":"skip","paired":False,"repeat":None,"policy":None,"arm":None})
    con.commit()
    con.close()
    trials[0].update(recorded_final_grade=False,submitted_public_exit=0,probe_status="mismatch",mismatched_cases=["case"],
                     replay_verdict_matches_record=True)
    ref = {"case":{"output":"expected"}}
    original = {"case":{"output":"changed"}}
    pair = {"expected":ref["case"],"observed":original["case"]}
    obs = {t:{"orig":{"cases":original},"ref":{"cases":ref}} for t in (COUNT,HINT)}
    obs[COUNT]["changed"] = {"cases":original,"status":"mismatch","differences":{"case":pair},
                            "independent_replay":{"timeout":False,"exit":1}}
    summary = [{k:trials[0][k] for k in ("run_id","task_id","repeat","policy","arm","actual_route",
                                          "patch_hash","submitted_public_exit","recorded_final_grade","mismatched_cases")}]
    p = {"schema":1,"result_hash":digest(r),"phase":r["phase"],"completed":r["completed"],"runtime_image":"img",
         "model_calls":0,"task_hashes":{t:{"reference":"ref","original":"orig","task":t+"hash"} for t in (COUNT,HINT)},
         "observations":obs,"probe_code":{t:"example" for t in (COUNT,HINT)},"trials":trials,
         "reference_cases":{t:ref for t in (COUNT,HINT)},"unique_submitted_patches":1,
         "run_counts":{"submitted":1,"no_submission":1,"mismatch":1},"counterexamples":summary}
    return tmp_path,r,p


def test_optional_probe_accepted_and_shows_unsubmitted_run(data):
    root, r, p = data
    mod = reporter()
    assert mod.checked_patch_probes(root,r) is None
    (root/"patch-probes.json").write_text(json.dumps(p))
    text = mod.checked_patch_probes(root,r)
    assert "1 次有差异" in text and "1 次求解没有可回放的提交" in text and "不影响门禁" in text


def test_compatibility_probe_is_reported_separately(data):
    root,r,p = data
    p = json.loads(json.dumps(p))
    extra = "count_with_explicit_bounds"
    p["probe_scope"] = {COUNT:{"case":"public_issue", extra:"upstream_compatibility"},HINT:{"case":"public_issue"}}
    p["reference_cases"][COUNT][extra] = {"output":"upstream"}
    p["observations"][COUNT]["ref"]["cases"][extra] = {"output":"upstream"}
    patch = p["observations"][COUNT]["changed"]
    patch["cases"] = {"case":{"output":"expected"}, extra:{"output":"different"}}
    patch["status"],patch["differences"] = "matches_reference",{}
    patch["compatibility_differences"] = {extra:{"observed":{"output":"different"},"expected":{"output":"upstream"}}}
    t = p["trials"][0]
    t.update(probe_status="matches_reference",mismatched_cases=[],compatibility_mismatched_cases=[extra])
    p["counterexamples"] = []
    p["compatibility_counterexamples"] = [{k:t[k] for k in ("run_id","task_id","repeat","policy","arm","actual_route",
                                                             "patch_hash","recorded_final_grade","compatibility_mismatched_cases")}]
    p["run_counts"]={"submitted":1,"no_submission":1,"matches_reference":1,"original_grade_fail_beyond_selected_public_cases":1}
    (root/"patch-probes.json").write_text(json.dumps(p))
    text = reporter().checked_patch_probes(root,r)
    assert "0 次有差异" in text and "兼容性检查发现 1 次" in text


def test_export_copies_and_links_probe(data,monkeypatch):
    root,r,p = data
    r.update(full_gate={"rows":[]},holdout={"rows":[]},run_costs={"calls":5,"cost_credits":1,"reserved_credits":0})
    p["result_hash"] = digest(r)
    (root/"result.json").write_text(json.dumps(r))
    (root/"patch-probes.json").write_text(json.dumps(p))
    output = root/"export"
    def base(_root,out):
        (out/"traces").mkdir(parents=True)
        (out/"report.html").write_text("<html></html>")
        (out/"report.md").write_text("# Test")
        for t in p["trials"]:
            (out/"traces"/(t["run_id"]+".json")).write_text(json.dumps({"run":t}))
    mod = reporter()
    monkeypatch.setattr(mod,"export_matrix",base)
    mod.export(root,output)
    assert (output/"patch-probes.json").read_bytes() == (root/"patch-probes.json").read_bytes()
    assert 'href="patch-probes.json"' in (output/"report.html").read_text()
    assert '](patch-probes.json)' in (output/"report.md").read_text()


@pytest.mark.parametrize("change", ["other_result","phase","holdout","wrong_patch","wrong_observation","wrong_counts"])
def test_probe_must_be_bound_to_this_result_and_focus_tasks(data, change):
    root,r,p = data
    if change == "other_result":
        p["result_hash"] = "different"
    elif change == "phase":
        p["phase"] = "complete"
    elif change == "holdout":
        p["probe_code"]["unseen"] = "bad"
    elif change == "wrong_patch":
        p["trials"][0]["patch_hash"] = "different"
    elif change == "wrong_observation":
        p["observations"][COUNT]["changed"]["differences"]["case"]["expected"] = {}
    else:
        p["run_counts"]["submitted"] = 2
    (root/"patch-probes.json").write_text(json.dumps(p))
    with pytest.raises(ValueError):
        reporter().checked_patch_probes(root,r)
