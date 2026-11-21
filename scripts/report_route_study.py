"""Export the completed GLM routing matrix without a provider connection."""
import argparse
import html
import json
import shutil
import sqlite3
from collections import Counter
from contextlib import closing
from pathlib import Path
from urllib.parse import quote

from evocoder.contracts import digest
from evocoder.route_study import export as export_matrix


def read(path):
    return json.loads(path.read_text())


def checked_patch_probes(root, result):
    source = root / "patch-probes.json"
    if not source.is_file():
        return None
    probe, protocol = read(source), result["protocol"]
    focus = set(protocol["tasks"])
    if (probe.get("schema") != 1 or probe.get("result_hash") != digest(result)
            or probe.get("phase") != result["phase"] or probe.get("completed") is not result["completed"]
            or probe.get("model_calls") != 0 or probe.get("runtime_image") != protocol["settings"]["image"]
            or focus != {"pallets-click-3d029e03eb", "pallets-click-42e2821ba3"}
            or any(set(probe[k]) != focus for k in ("task_hashes","observations","probe_code","reference_cases"))):
        raise ValueError("post-hoc probe is not bound to this terminal result and its two development cases")
    pairs = {r[arm+"_run"]:(r,arm) for r in result["rows"] for arm in ("baseline","candidate")}
    all_ids = set(result["run_ids"])
    # Avoid opening solver logs or task files outside the two diagnosed tasks.
    dbfile = (root / "seed" / "evocoder.sqlite3").resolve()
    wal = dbfile.with_name(dbfile.name+"-wal")
    query = "?mode=ro" if wal.exists() and wal.stat().st_size else "?mode=ro&immutable=1"
    with closing(sqlite3.connect(dbfile.as_uri()+query, uri=True)) as db:
        expected = {key:(task,task_hash,state,skill,json.loads(body)) for key,task,task_hash,state,skill,body in db.execute(
            "SELECT id,task_id,task_hash,state,skill_id,result FROM runs "
            "WHERE split='dev' AND purpose='evaluate' AND task_id IN (?,?)", tuple(focus)) if key in all_ids}
    scopes = probe.get("probe_scope", {})
    if scopes:
        expected_scopes = {t:{k:("upstream_compatibility"
                                  if (t,k)==("pallets-click-3d029e03eb","count_with_explicit_bounds")
                                  else "public_issue") for k in probe["reference_cases"][t]} for t in focus}
        if scopes != expected_scopes:
            raise ValueError("probe mixed the public issue with compatibility cases")
    counts, seen, unique, mismatches, compatibility = Counter(), set(), set(), [], []
    for trial in probe["trials"]:
        key, task = trial["run_id"], trial["task_id"]
        if key in seen or key not in expected or task not in focus:
            raise ValueError("unexpected or duplicate development trial in patch probe")
        seen.add(key)
        tid, h, state, skill, outcome = expected[key]
        patch = trial["patch_hash"]
        if (task != tid or h != probe["task_hashes"][task]["task"] or patch != outcome.get("patch")
                or state != trial["state"] or skill != trial["applied_skill"]
                or trial["passed"] != outcome.get("passed") or trial["paired"] is not (key in pairs)):
            raise ValueError("probe trial disagrees with recorded development run")
        if key in pairs:
            row, arm = pairs[key]
            if (trial["arm"],trial["repeat"],trial["policy"],trial["actual_route"]) != (
                    arm,row["repeat"]+1,row["policy"],row[arm+"_route"]):
                raise ValueError("probe trial disagrees with paired assignment")
        counts["submitted" if patch else "no_submission"] += 1
        if not patch:
            continue
        unique.add((task,patch))
        obs = probe["observations"][task][patch]
        if ("cases" in obs and obs["cases"].keys() != probe["reference_cases"][task].keys()):
            raise ValueError("probe output cases differ from task reference")
        all_differences = {k:{"observed":v,"expected":probe["reference_cases"][task][k]}
                           for k,v in obs.get("cases", {}).items() if v != probe["reference_cases"][task][k]}
        extra = {k:v for k,v in all_differences.items() if scopes.get(task,{}).get(k)=="upstream_compatibility"}
        differences = {k:v for k,v in all_differences.items() if k not in extra}
        differing = sorted(differences)
        status = ("mismatch" if differing else "matches_reference") if "cases" in obs else "probe_failed"
        if (obs["status"] != status or trial["probe_status"] != status or differing != trial["mismatched_cases"]
                or obs["differences"] != differences or outcome["passed"] is not trial["recorded_final_grade"]):
            raise ValueError("patch probe comparison disagrees with observed output")
        if extra != obs.get("compatibility_differences",{}) or sorted(extra) != trial.get("compatibility_mismatched_cases",[]):
            raise ValueError("compatibility probe results differ from the observed patch")
        if extra:
            compatibility.append({k:trial[k] for k in ("run_id","task_id","repeat","policy","arm","actual_route",
                                                       "patch_hash","recorded_final_grade","compatibility_mismatched_cases")})
        counts[status] += 1
        if differing:
            mismatches.append({k:trial[k] for k in ("run_id","task_id","repeat","policy","arm","actual_route",
                                                     "patch_hash","submitted_public_exit","recorded_final_grade","mismatched_cases")})
        regrade = obs["independent_replay"]
        agrees = (not regrade["timeout"] and
                  ((trial["submitted_public_exit"] == 0 and regrade["exit"] == 0) == trial["recorded_final_grade"]))
        if agrees != trial["replay_verdict_matches_record"]:
            raise ValueError("probe regraded verdict disagrees with receipt")
        counts["replayed_verdict_disagreement"] += int(not agrees)
        if status == "mismatch" and trial["recorded_final_grade"]:
            counts["original_grade_pass_with_public_probe_mismatch"] += 1
        if status == "matches_reference" and not trial["recorded_final_grade"]:
            counts["original_grade_fail_beyond_selected_public_cases"] += 1
    if (seen != set(expected) or counts != Counter(probe["run_counts"]) or len(unique) != probe["unique_submitted_patches"]
            or mismatches != probe["counterexamples"] or compatibility != probe.get("compatibility_counterexamples",[])):
        raise ValueError("post-hoc probe totals disagree with recorded trials")
    for task in focus:
        hashes = probe["task_hashes"][task]
        obs = probe["observations"][task]
        if (set(obs) != {hashes["reference"],hashes["original"]} | {h for tid,h in unique if tid == task}
                or obs[hashes["reference"]]["cases"] != probe["reference_cases"][task]):
            raise ValueError("unexpected probe patch or reference case")
    compat_note = (f"独立列出的上游显式计数范围兼容性检查发现 {len(compatibility)} 次提交行为不同。" if scopes else "")
    return (f"离线复查覆盖 {counts['submitted']} 次提交、{len(unique)} 份不同补丁。"
            f"在选定的公开行为示例上，{counts['matches_reference']} 次提交与上游修复一致，"
            f"{counts['mismatch']} 次有差异，{counts['probe_failed']} 次探针运行失败。"
            f"另有 {counts['no_submission']} 次求解没有可回放的提交。"
            f"独立评分回放与原记录不一致 {counts['replayed_verdict_disagreement']} 次。"
            f"{compat_note}这些探针用于解释已观察到的输出差异，不影响门禁或留出评测。")


def export(root, output):
    """Extend the original matrix report with interruption evidence and cumulative cost."""
    export_matrix(root, output)
    result = read(root / "result.json")
    probe_note = checked_patch_probes(root, result)
    continuation = root / "continuation.json"
    links = [("冻结协议", "protocol.json"), ("实际路由请求取证", "historical-routes.json"), ("终态记录", "result.json")]
    notes = []
    if continuation.is_file():
        binding = read(continuation)
        assert result["continuation_hash"] == digest(binding)
        previous = Path(binding["previous"]) / "result.json"
        assert digest(read(previous)) == binding["result_hash"]
        assert digest((root / "continuation-driver.py").read_text()) == binding["driver_hash"]
        for name, title in (("continuation.json", "续跑来源与预算快照"), ("continuation-driver.py", "当时的续跑脚本"),
                            ("retry-events.json", "平台限流尝试")):
            if (root / name).is_file():
                shutil.copy2(root/name,output/name)
                links.append((title,name))
        shutil.copy2(previous,output/"interrupted-source-result.json")
        links.append(("原中断记录", "interrupted-source-result.json"))
        notes.append(f"从 {Path(binding['previous']).name} 续跑，复用 {len(result['reused_terminal_runs'])} 条已终止的求解轨迹。"
                     f"续跑新增已结算 {result['continuation_costs']['cost_credits']:.5f} 积分，"
                     f"新增未决预留 {result['continuation_costs']['reserved_credits']:.5f} 积分。")
    totals = result["run_costs"]
    notes.append(f"从本次协议首次启动累计登记 {totals['calls']} 个模型请求，"
                 f"已结算用量估算为 {totals['cost_credits']:.5f} 积分，还有 {totals['reserved_credits']:.5f} 积分预留。"
                 "已结算统计包含错误尝试在中断前已完成的调用，未决金额等待用量证据。")
    pairs = result["rows"] + result["full_gate"]["rows"] + result["holdout"]["rows"]
    scored = {row[arm+"_run"] for row in pairs for arm in ("baseline", "candidate")}
    errored = []
    for key in result["run_ids"]:
        if key in scored:
            continue
        run = read(output/"traces"/(key+".json"))["run"]
        relative = "traces/" + quote(key, safe="") + ".json"
        errored.append((f"{run['task_id']} · {run['state']} · {key}", relative))
    intro = "未计入完整配对的运行" if errored else "所有终态求解轨迹均已计入配对"
    def anchors(items):
        return " · ".join(f'<a href="{html.escape(href,quote=True)}">{html.escape(label)}</a>' for label,href in items)
    def md_links(items):
        return " · ".join(f"[{label}]({href})" for label,href in items)
    section = "<h2>中断证据与累计用量</h2>" + "".join("<p>"+html.escape(n)+"</p>" for n in notes)
    section += "<p>"+anchors(links)+"</p><p>"+intro+"</p>"
    if errored:
        section += "<ul>"+"".join("<li>"+anchors([item])+"</li>" for item in errored)+"</ul>"
    if reused := result.get("reused_terminal_runs"):
        section += "<p>沿用的旧终态运行 "+anchors([(key,"traces/"+quote(key,safe="")+".json") for key in reused])+"</p>"
    probe_link = [("补丁、实际输出与测试回放", "patch-probes.json")]
    if probe_note:
        shutil.copy2(root/"patch-probes.json", output/"patch-probes.json")
        section += "<h2>公开行为探针</h2><p>"+html.escape(probe_note)+"</p><p>"+anchors(probe_link)+"</p>"
    page = output/"report.html"
    page.write_text(page.read_text().replace("</html>",section+"</html>"))
    md = ["## 中断证据与累计用量", *notes, md_links(links), intro]
    if errored:
        md.append("\n".join("- " + md_links([item]) for item in errored))
    if reused:
        md.append("沿用的旧终态运行 " + md_links([(key,"traces/"+quote(key,safe="")+".json") for key in reused]))
    if probe_note:
        md.extend(["## 公开行为探针",probe_note,md_links(probe_link)])
    with (output / "report.md").open("a") as handle:
        handle.write("\n"+"\n\n".join(md)+"\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    export(args.root, args.output)
