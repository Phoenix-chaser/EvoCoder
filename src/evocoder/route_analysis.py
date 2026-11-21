"""Paired diagnostics for a frozen skill family under semantic and fixed routing."""

import html
from collections import Counter
from decimal import Decimal
from pathlib import Path
from urllib.parse import quote

from evocoder.gate import gate_summary, skill_behavior


POLICIES = ("normal", "fixed_apply", "fixed_skip")
ARMS = ("baseline", "candidate")
LABELS = {"normal": "正常路由", "fixed_apply": "固定应用", "fixed_skip": "固定跳过"}
REASONS = {
    "incomplete_matrix": "尚未完成诊断矩阵",
    "infrastructure_error": "存在基础设施错误",
    "insufficient_repeated_wins": "两道焦点题均未形成重复改善，剩余一个开发分组无法补足正式门禁所需的两个分组",
    "normal_regression": "正常路由出现退化",
}


def _summary(rows):
    out = {"pairs": len(rows), "wins": 0, "losses": 0, "both_pass": 0, "both_fail": 0}
    for row in rows:
        outcomes = (row["baseline_pass"], row["candidate_pass"])
        key = {(True, True): "both_pass", (False, False): "both_fail",
               (False, True): "wins", (True, False): "losses"}[outcomes]
        out[key] += 1
    for arm in ARMS:
        out[arm] = {
            "passed": sum(row[f"{arm}_pass"] for row in rows),
            "applied": sum(row[f"{arm}_applied_skill"] is not None for row in rows),
            "routes": dict(Counter(row[f"{arm}_route"] for row in rows)),
        }
        for key in ("model_steps", "budget_steps", "credits"):
            values = [row.get(f"{arm}_{key}") for row in rows]
            if any(value is None for value in values):
                out[arm][key] = None
            elif key == "credits":
                out[arm][key] = float(sum(Decimal(str(value)) for value in values))
            else:
                out[arm][key] = sum(values)
    return out


def _attribution(rows):
    by_rep = {}
    for row in rows:
        by_rep.setdefault(row["repeat"], {})[row["policy"]] = row
    paired = [(r["fixed_apply"], r["fixed_skip"]) for r in by_rep.values()
              if "fixed_apply" in r and "fixed_skip" in r]
    result = {}
    for arm in ARMS:
        normal = [items["normal"] for _, items in sorted(by_rep.items()) if "normal" in items]
        result[arm] = {
            "normal_routes": [{"repeat": r["repeat"], "decision": r[f"{arm}_route"],
                               "applied": r[f"{arm}_applied_skill"] is not None,
                               "passed": r[f"{arm}_pass"]} for r in normal],
            "normal_route_changed": len({r[f"{arm}_route"] for r in normal}) > 1,
            "normal_injection_changed": len({r[f"{arm}_applied_skill"] for r in normal}) > 1,
            "fixed_comparisons": len(paired),
            "only_apply_pass": sum(a[f"{arm}_pass"] and not s[f"{arm}_pass"] for a, s in paired),
            "only_skip_pass": sum(s[f"{arm}_pass"] and not a[f"{arm}_pass"] for a, s in paired),
        }
    return result


def analyze_routes(rows: list[dict], *, tasks: dict[str, str], parent: str, child: str,
                   repeats: int = 2) -> dict:
    if len(tasks) != 2 or len(set(tasks.values())) != 2 or repeats != 2 or not parent or not child or parent == child:
        raise ValueError("route diagnosis requires two independent tasks, two repeats, and distinct versions")
    seen, run_ids, hashes = set(), set(), {}
    validated = []
    for row in rows:
        task, policy, repeat = row["task_id"], row["policy"], row["repeat"]
        if (task not in tasks or row["source_group"] != tasks[task] or row["split"] != "dev"
                or policy not in POLICIES or type(repeat) is not int or not 0 <= repeat < repeats
                or (task, policy, repeat) in seen):
            raise ValueError("unexpected or duplicate route-diagnostic pair")
        if not row["task_hash"] or hashes.setdefault(task, row["task_hash"]) != row["task_hash"]:
            raise ValueError("task snapshot changed between paired runs")
        if type(row["infrastructure_error"]) is not bool:
            raise ValueError("missing infrastructure outcome")
        seen.add((task, policy, repeat))
        for arm, version in (("baseline", parent), ("candidate", child)):
            run = row[f"{arm}_run"]
            if not run or run in run_ids or type(row[f"{arm}_pass"]) is not bool:
                raise ValueError("reused or invalid evaluation run")
            run_ids.add(run)
            route = row[f"{arm}_route"]
            skill = row[f"{arm}_applied_skill"]
            if route not in {"apply", "skip", "uncertain", "skip_invalid", "skip_memory_budget"}:
                raise ValueError("unexpected route decision")
            if skill != (version if route == "apply" else None):
                raise ValueError("route disagrees with actual version injected")
            if policy != "normal" and route != policy.removeprefix("fixed_"):
                raise ValueError("fixed intervention did not bind its assigned route")
            if f"{arm}_route_source" in row:
                forced = row[f"{arm}_route_source"] == "fixed_intervention"
                if forced != (policy != "normal"):
                    raise ValueError("router provenance disagrees with assigned intervention")
        if policy == "fixed_skip" and row.get("baseline_solver_memory_hash") != row.get("candidate_solver_memory_hash"):
            raise ValueError("fixed skip versions received different solver memories")
        validated.append({**row, "skill_behavior": skill_behavior(row, child, {parent})})

    subsets = {p: [r for r in validated if r["policy"] == p] for p in POLICIES}
    normal = gate_summary(subsets["normal"], repeats)
    fixed_losses = [dict(task_id=r["task_id"], repeat=r["repeat"]) for r in subsets["fixed_apply"]
                    if r["baseline_pass"] and not r["candidate_pass"]]
    issues = []
    expected = len(tasks) * len(POLICIES) * repeats
    if len(rows) != expected:
        issues.append("incomplete_matrix")
    if any(r["infrastructure_error"] for r in validated):
        issues.append("infrastructure_error")
    if not normal["repeat_confirmed_improved_groups"]:
        issues.append("insufficient_repeated_wins")
    if normal["regressions"]:
        issues.append("normal_regression")

    by_task, attribution = {}, {}
    for task in tasks:
        by_task[task] = {p: _summary([r for r in subsets[p] if r["task_id"] == task]) for p in POLICIES}
        attribution[task] = _attribution([r for r in validated if r["task_id"] == task])
    return {
        "scope": "previously_exposed_development_tasks", "expected_pairs": expected, "pairs": len(rows),
        "gate": {"passed": not issues, "kind": "eligible_to_complete_original_normal_gate",
                 "remaining_dev_groups": 1, "complete": len(rows) == expected, "blockers": issues,
                 "normal": normal, "fixed_apply_regressions": fixed_losses},
        "by_policy": {p: _summary(subsets[p]) for p in POLICIES},
        "by_task": by_task, "attribution": attribution,
    }


def write_route_report(output: Path, protocol: dict, result: dict) -> None:
    """Export final or partial evidence; row trace links are provided by the run exporter."""
    output.mkdir(parents=True, exist_ok=True)
    rows = result.get("rows", [])
    info = analyze_routes(rows, tasks=protocol["tasks"], parent=protocol["parent"],
                          child=protocol["child"], repeats=protocol["repeats"])
    if result.get("analysis") is not None and result["analysis"] != info:
        raise ValueError("recorded route analysis disagrees with the paired rows")
    body, md = [], []

    def heading(title):
        body.append("<h2>" + html.escape(title) + "</h2>")
        md.append("## " + title)

    def paragraph(line):
        body.append("<p>" + html.escape(line) + "</p>")
        md.append(line)

    def cells(values, *, header=False):
        tag = "th" if header else "td"
        h, m = [], []
        for cell in values:
            if isinstance(cell, tuple):
                label, run = cell
                href = "traces/" + quote(str(run), safe="") + ".json"
                h.append(f'<a href="{html.escape(href, quote=True)}">{html.escape(label)}</a>')
                m.append(f"[{label}]({href})")
            else:
                h.append(html.escape(str(cell)))
                m.append(str(cell).replace("|", "\\|"))
        return "<tr>" + "".join(f"<{tag}>{v}</{tag}>" for v in h) + "</tr>", "| " + " | ".join(m) + " |"

    def table(headers, values):
        hr, mr = cells(headers, header=True)
        rendered = [cells(v) for v in values]
        body.append('<div class="scroll"><table><thead>' + hr + "</thead><tbody>" +
                    "".join(h for h, _ in rendered) + "</tbody></table></div>")
        md.append("\n".join([mr, "| " + " | ".join("---" for _ in headers) + " |",
                             *(m for _, m in rendered)]))

    def metric(values):
        return f"{values:.5f}" if isinstance(values, float) else "未记录" if values is None else str(values)

    def fraction(value, pairs):
        return f"{value}/{pairs}" if pairs else "—"

    status = "已完成" if result.get("completed") else "已中断" if result.get("error") else "进行中"
    title = "Click 技能路由对照"
    heading("实验范围")
    paragraph(f"{status}。完成 {len(rows)}/{info['expected_pairs']} 对诊断。两道题均在历史开发实验中暴露过。")
    labels = {"pallets-click-3d029e03eb": "计数选项的范围后缀", "pallets-click-42e2821ba3": "Click #557 的帮助提示"}
    settings = protocol["settings"]
    paragraph(f"父版 {protocol['parent']}。子版 {protocol['child']}。模型 {settings['model']}，"
              f"温度 {settings['temperature']}，每次最多 {settings['max_steps']} 步。固定路由保留一步路由预算。")
    paragraph("每种策略独立求解同一快照。固定应用比较两个技能版本在都已注入时的表现。"
              "固定跳过给两个求解器空技能记忆，同题结果出现差异时可观察运行波动。")
    if result.get("error"):
        paragraph("停止原因 " + str(result["error"]))

    heading("正常与固定路由的独立评分")
    totals = []
    for policy, s in info["by_policy"].items():
        total = s["pairs"]
        before, after = s["baseline"], s["candidate"]
        totals.append([LABELS[policy], total, fraction(before["passed"], total), fraction(after["passed"], total),
                       before["applied"], after["applied"], s["wins"], s["losses"],
                       metric(before["credits"]), metric(after["credits"])])
    table(["策略", "配对", "父通过", "子通过", "父注入", "子注入", "子独有成功", "父独有成功", "父积分", "子积分"], totals)
    for task, subsets in info["by_task"].items():
        heading(labels.get(task, task))
        table(["策略", "父通过", "子通过", "子独有成功", "父独有成功", "两边通过", "两边失败"],
              [[LABELS[p], fraction(s["baseline"]["passed"], s["pairs"]),
                fraction(s["candidate"]["passed"], s["pairs"]), s["wins"], s["losses"],
                s["both_pass"], s["both_fail"]] for p, s in subsets.items()])
        for arm, name in (("baseline", "父版"), ("candidate", "子版")):
            attribution = info["attribution"][task][arm]
            route_text = "、".join("第{}次 {}（{}）".format(
                t["repeat"] + 1, t["decision"], "通过" if t["passed"] else "失败"
            ) for t in attribution["normal_routes"]) or "尚无完整配对"
            paragraph(f"{name}正常路由为{route_text}。按重复编号并列比较固定应用与固定跳过，"
                      f"{attribution['fixed_comparisons']} 次中仅应用通过 {attribution['only_apply_pass']} 次，"
                      f"仅跳过通过 {attribution['only_skip_pass']} 次。")
        noise = subsets["fixed_skip"]["wins"] + subsets["fixed_skip"]["losses"]
        paragraph(f"固定跳过的 {subsets['fixed_skip']['pairs']} 对中，同样空技能记忆得出不同通过结果 {noise} 对。"
                  "每种干预只有两次重复，工具轨迹与补丁用来解释个案，单次差异不能识别稳定的因果效应。")

    heading("门禁与留出评测")
    gate = info["gate"]
    normal = gate["normal"]
    paragraph(f"补跑完整开发门禁的条件{'已满足' if gate['passed'] else '未满足' if gate['complete'] else '待确认'}。"
              f"两道焦点题中，正常路由重复改善且技能行为发生变化的分组为 {len(normal['repeat_confirmed_improved_groups'])}/2，"
              f"正常路由退化任务 {len(normal['regressions'])} 道，固定应用退化 {len(gate['fixed_apply_regressions'])} 对。")
    if gate["blockers"]:
        paragraph("当前阻断条件包括" + "、".join(REASONS[k] for k in gate["blockers"]) + "。")
    paragraph("原协议共三个开发分组，正式门禁要求至少两个分组各重复改善两次，且零退化。"
              "两道焦点题至少需要贡献一个这样的分组，余下开发题才有机会补足。"
              "固定路由的胜负仅用于诊断，不计入正式门禁。")
    full = result.get("full_gate") or {}
    holdout = result.get("holdout") or {}
    extra, held = full.get("rows", []), holdout.get("rows", [])
    paragraph(f"完整开发门禁状态 {full.get('status', '尚未启动')}，登记 {len(extra)} 对。"
              f"留出状态 {holdout.get('status', '尚未启动')}，已完成 {len(held)} 对。"
              "只有完整开发门禁通过，才会用预留的新题评估迁移收益。")
    if held:
        s = _summary(held)
        paragraph(f"留出题父版通过 {s['baseline']['passed']}/{len(held)}，子版通过 {s['candidate']['passed']}/{len(held)}。")
    heading("逐例轨迹与用量")
    def trace_rows(records, phase):
        for r in records:
            cells_for_pair = [phase, labels.get(r["task_id"], r["task_id"]), r["repeat"] + 1,
                              LABELS.get(r.get("policy", "normal"), r.get("policy"))]
            for arm in ARMS:
                cells_for_pair.append((f"{r[arm+'_route']} · " + ("通过" if r[arm+"_pass"] else "失败"), r[arm+"_run"]))
                cells_for_pair.append(metric(r.get(arm + "_model_steps")))
            yield cells_for_pair

    detailed = list(trace_rows(rows, "诊断"))
    already = {(row["baseline_run"], row["candidate_run"]) for row in rows}
    detailed.extend(trace_rows([r for r in extra if (r["baseline_run"], r["candidate_run"]) not in already], "开发回放"))
    detailed.extend(trace_rows(held, "留出"))
    table(["阶段", "任务", "重复", "策略", "父版轨迹", "父模型步数", "子版轨迹", "子模型步数"], detailed)
    total_cost = result.get("new_cost_credits")
    if total_cost is not None:
        paragraph(f"本轮新增已结算用量估算为 {metric(total_cost)} 套餐积分。汇总口径包含本轮所有已派发且可结算请求。")

    (output / "report.md").write_text("# " + title + "\n\n" + "\n\n".join(md) + "\n")
    style = ("body{font:16px/1.65 system-ui;max-width:1150px;margin:2rem auto;padding:0 1rem;color:#192734}"
             "p{overflow-wrap:anywhere}h2{margin-top:2em}.scroll{overflow-x:auto}table{border-collapse:collapse}"
             "td,th{padding:.35rem .55rem;border:1px solid #c7d0d9;white-space:nowrap}th{background:#eef2f5}"
             "a{color:#194e80}")
    (output / "report.html").write_text('<!doctype html><html lang="zh-CN"><meta charset="utf-8">' +
        '<meta name="viewport" content="width=device-width, initial-scale=1"><title>' + title +
        "</title><style>" + style + "</style><h1>" + title + "</h1>" + "\n".join(body) + "</html>")
