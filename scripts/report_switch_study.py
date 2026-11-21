"""Build the Chinese study report or replay its portable GLM assertion with zero model calls."""
import argparse
from html import escape
from pathlib import Path

from evocoder.comparison import dump
from evocoder.contracts import Settings, digest
from evocoder.revision_diagnostic import read
from evocoder.runtime import validation
from evocoder.switch_feedback import axis_from_public
from prepare_click_procedure import task_case
from prepare_switch_learning import CASES, off_mutant
from replay_click_example import apply, edits
from report_click_procedure import Archive
from verify_authored_cli_checks import ENV, entries
from verify_switch_trials import observed_run, strict_result

NAMES = {"c1b34978f1": "Choice 原始拼写", "8d7f03dac8": "自动环境变量", "3d029e03eb": "计数选项",
         "42e2821ba3": "Click #557"}
ROLES = {"baseline": "父版", "candidate": "子版"}


def evidence(root):
    p, r = read(root / "protocol.json"), read(root / "result.json")
    if not r["completed"] or r["protocol"] != digest(p) or digest(read(root / "controls.json")) != p["controls_hash"]:
        raise ValueError("completed, bound study required")
    rows = []
    for phase, pairs in (("学习", r["learning"]), ("开发", r["rows"])):
        for pair in pairs:
            for arm, name in ROLES.items():
                trial = read(root / "assertions" / (pair[arm + "_run"] + ".json"))
                if (digest(trial) != pair[arm + "_behavior_hash"] or trial["task"] != pair["task_id"]
                        or trial["passed"] != pair[arm + "_pass"] or trial["route"] != pair[arm + "_route"]
                        or trial["model_steps"] > p["settings"]["max_steps"]):
                    raise ValueError("result and execution receipt disagree")
                rows.append({**trial, "phase": phase, "role": name, "repeat": pair["repeat"] + 1})
    return p, r, rows


def portable(root, p, rows):
    # Illustration only. Choose the first learning action that received a measured single-state gap and caught it.
    choices = [(t, c) for t in rows if t["phase"] == "学习" for c in t["checks"]
               if c["detected"] and c["feedback_before"]["not_yet_observed"] == ["disabled"]]
    if not choices:
        return None
    t, c = choices[0]
    archive = Archive(root / "seed/evocoder.sqlite3")
    events = {e["seq"]: e for e in archive.events(t["run"])}
    action = events[c["seq"]]["payload"]
    meta = events[c["seq"] + 1]["payload"]
    observed = events[c["seq"] + 2]["payload"]["observation"]
    task, ref = entries(Path(p["learn_manifest"]), [t["task"]])[t["task"]]
    original = {k: task.files[k] for k in task.editable}
    patch = archive.get_artifact(c["patch"])
    sample = {"protocol": digest(p), "model": p["settings"]["model"], "image": p["settings"]["image"],
              "run": t["run"], "task": t["task"], "role": t["role"], "route": t["route"], "step": c["step"],
              "budget": p["settings"]["max_steps"], "feedback_before": c["feedback_before"], "source": c["source"],
              "argv": [*ENV, c["source"]] if action["tool"] == "cli_check" else action["argv"],
              "task_hash": digest(task.model_dump()), "reference_hash": digest(ref), "mutant_hash": digest(off_mutant(task, ref)),
              "patch_hash": c["patch"], "edits": edits(original, patch), "output_hash": digest(observed["output"]),
              "exit": observed["exit_code"], "argv_hash": meta["argv_hash"], "source_hash": c["source_hash"],
              "assertion_line": c["authored_assertion_line"], "off_call_indices": c["off_call_indices"]}
    return {"example": sample, "hash": digest(sample)}


def replay(example, repo):
    e = read(example)
    x = e["example"]
    if digest(x) != e["hash"] or digest(x["argv"]) != x["argv_hash"] or digest(x["source"]) != x["source_hash"]:
        raise ValueError("example digest mismatch")
    spec, = [spec for spec in CASES if x["task"].endswith(spec["commit"][:10])]
    task, meta = task_case(repo, spec)
    patch = apply({k: task.files[k] for k in task.editable}, x["edits"])
    ref = meta["reference"]
    bad = off_mutant(task, ref)
    if [digest(v) for v in (task.model_dump(), patch, ref, bad)] != [
        x["task_hash"], x["patch_hash"], x["reference_hash"], x["mutant_hash"]
    ] or x["feedback_before"]["patch"] != x["patch_hash"] or not 1 <= x["step"] <= x["budget"]:
        raise ValueError("task, patch or turn binding mismatch")
    opts = Settings(_env_file=None, image=x["image"])
    raw = validation({**task.files, **patch}, x["argv"], opts, "portable-switch-raw")
    matches = (raw["image_id"] == x["image"] and not raw["timeout"]
               and raw["exit_code"] == x["exit"] and digest(raw["output"][:10000]) == x["output_hash"])
    axis = axis_from_public(task.files, task.public_command, task.issue)
    logs = [observed_run(task, tree, x["source"], axis, opts) for tree in (patch, ref, bad)]
    result = strict_result(*logs, x["source"], matches, x["exit"])
    if not result["detected"] or result["authored_assertion_line"] != x["assertion_line"] or result["off_call_indices"] != x["off_call_indices"]:
        raise ValueError("could not reproduce archived assertion")
    return {**result, "model_calls": 0, "step": x["step"], "exits": [v["exit"] for v in logs]}


def render(root, out, example=None):
    p, r, rows = evidence(root)
    out.mkdir(parents=True, exist_ok=True)
    def yn(value):
        return "是" if value else "否"

    parts = ['<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width">'
             '<title>EvoCoder · 16 步内的开关覆盖实验</title><style>body{font:16px/1.65 system-ui,sans-serif;color:#1e2731;'
             'max-width:1100px;margin:2.5rem auto;padding:0 1rem}table{border-collapse:collapse;width:100%}'
             'td,th{padding:.38rem .55rem;border-bottom:1px solid #d1d8df;text-align:left}th{background:#edf3f4}'
             '.scroll{overflow:auto}pre{background:#f4f6f8;padding:1rem;overflow:auto}details{margin:.5rem 0}'
             'small{color:#4d5966}summary{cursor:pointer}h2{margin-top:2rem}</style><main>'
             '<h1>GLM 能在 16 步内写出能检出关闭回归的断言吗？</h1>']
    for phase in ("学习", "开发"):
        group = [t for t in rows if t["phase"] == phase]
        parts.append("<h2>" + {"学习": "独立学习题", "开发": "正常路由父子对照"}[phase] + "</h2>")
        parts.append('<div class="scroll"><table><thead><tr><th>任务</th><th>轮次</th><th>版本</th><th>路由</th>'
                     '<th>总步数</th><th>自写检查跑到关闭侧</th><th>首次检出步骤</th><th>提交版本上检出</th>'
                     '<th>修复通过</th></tr></thead><tbody>')
        for t in group:
            first = next((str(c["step"]) for c in t["checks"] if c["detected"]), "—")
            fields = [NAMES.get(t["task"].split("-")[-1], t["task"]), t["repeat"], t["role"], t["route"],
                      t["model_steps"], yn(t["authored_off_observed"]), first, yn(t["detected_on_submission"]), yn(t["passed"])]
            parts.append("<tr>" + "".join("<td>" + escape(str(v)) + "</td>" for v in fields) + "</tr>")
        parts.append("</tbody></table></div>")
    parts.append("<h2>门禁与费用</h2>")
    gate = r["gate"] if r["gate"]["status"] == "complete" else r["focus_gate"]
    parts.append(f"<p>重复确认改善的独立开发组 {len(gate['repeat_confirmed_improved_groups'])} 个，"
                 f"发生退化的任务 {len(gate['regressions'])} 个。门禁要求至少两个组各连续两次配对改善，且回归与基础设施错误均为零。"
                 f"留出配对完成 {len(r['holdout']['rows'])} 次。本轮调用 {r['costs']['calls']} 次，"
                 f"按项目账本记 {r['costs']['cost_credits']:.2f} 套餐积分。</p>")
    later = [(x["evaluation_role"], x) for x in r["gate"]["rows"] if x.get("evaluation_role")]
    later += [("留出题", x) for x in r["holdout"]["rows"]]
    if later:
        parts.append("<table><tr><th>阶段</th><th>任务</th><th>轮次</th><th>父版修复</th><th>子版修复</th></tr>")
        for phase, pair in later:
            fields = [phase, pair["task_id"], pair["repeat"] + 1, yn(pair["baseline_pass"]), yn(pair["candidate_pass"])]
            parts.append("<tr>" + "".join("<td>" + escape(str(v)) + "</td>" for v in fields) + "</tr>")
        parts.append("</table>")
    parts.append("<p>每次求解含语义路由，上限 16 步。学习题每个版本各一次，开发题各两次。两版同享覆盖反馈。"
                 "公开测试、自写检查与补丁独立评分分别记账。检出须同时复现当时的执行结果、在上游版本通过，"
                 "并在只改变关闭行为的对照版本触发自写断言。本轮未设置关闭反馈的配对组。</p>"
                 "<h2>逐次自写检查</h2>")
    for t in rows:
        title = f"{NAMES[t['task'].split('-')[-1]]} · {t['role']} · 第 {t['repeat']} 轮"
        parts.append(f"<details><summary>{escape(title)}（{len(t['checks'])} 次）</summary><p>运行 <code>{t['run']}</code></p>")
        for c in t["checks"]:
            missing = ", ".join(c["feedback_before"]["not_yet_observed"]) or "无"
            parts.append(f"<p>第 {c['step']} 步，行动前未观察到 {escape(missing)}。原执行码 {c['live_exit']}，"
                         f"上游执行码 {c['reference']['exit']}，关闭回归执行码 {c['off_mutant']['exit']}。"
                         f"严格检出 {yn(c['detected'])}，断言行 {c['authored_assertion_line'] or '—'}。</p>"
                         "<pre>" + escape(c["source"]) + "</pre>")
        parts.append("</details>")
    parts.append("<p><small>模型 " + escape(p["settings"]["model"]) + " · 协议摘要 " + digest(p) + "</small></p></main></html>")
    (out / "report.html").write_text("\n".join(parts))
    dump(out / "trials.json", {"protocol": digest(p), "rows": rows, "focus_gate": r["focus_gate"], "gate": r["gate"],
                               "holdout": r["holdout"], "costs": r["costs"]})
    if example is not None and (sample := portable(root, p, rows)):
        dump(example, sample)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="command", required=True)
    report = sub.add_parser("report")
    report.add_argument("--root", type=Path, required=True)
    report.add_argument("--output", type=Path, required=True)
    report.add_argument("--example", type=Path)
    check = sub.add_parser("replay")
    check.add_argument("--example", type=Path, default=Path("examples/click-live-switch.json"))
    check.add_argument("--repo", type=Path, default=Path("work/upstream/click.git"))
    args = ap.parse_args()
    if args.command == "report":
        render(args.root, args.output, args.example)
    else:
        print(replay(args.example, args.repo))
