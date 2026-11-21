"""Audit both frozen experiments and the separately recorded failed proposal attempt."""
from __future__ import annotations

import html
import json
import shutil
from collections import Counter
from pathlib import Path

from evocoder.contracts import Settings, digest
from evocoder.evolution import Evolution
from evocoder.store import Store


def dump(path, body):
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(body, ensure_ascii=False, indent=2) + "\n"
    settings = Settings()
    if settings.api_key and settings.api_key in text:
        raise ValueError("credential found in export")
    path.write_text(text)


def table(headers, rows):
    def cells(items, tag):
        return "".join(f"<{tag}>{html.escape(str(item))}</{tag}>" for item in items)
    return ("<div class='table'><table><thead><tr>" + cells(headers, "th") +
            "</tr></thead><tbody>" + "".join("<tr>" + cells(row, "td") + "</tr>" for row in rows) +
            "</tbody></table></div>")


def main():
    settings = Settings()
    store = Store(settings.state_dir)
    second = json.loads(Path("experiments/live-002/summary.json").read_text())
    revised = json.loads(Path("experiments/live-004/result.json").read_text())
    invalid_protocol = json.loads(Path("experiments/live-003/protocol.json").read_text())
    after = store.budget()
    with store.connect() as db:
        calls = [dict(r) for r in db.execute("SELECT * FROM calls ORDER BY rowid")]
    usage = [json.loads(r["usage"]) for r in calls if r["usage"]]
    summary = {"schema": 1, "mode": "live", "model": settings.model,
               "claim": "exploratory_selfbuilt_same_domain_feedback_revision",
               "strict_policy_pilot": second,
               "rejected_reference_attempt": {"protocol": invalid_protocol,
                    "error": "candidate cited evidence outside frozen pool",
                    "original_candidate_body_retained": False,
                    "cost_usd": round(revised["budget_before"]["spent_usd"] -
                                      invalid_protocol["budget_before"]["spent_usd"], 12)},
               "feedback_revision_pilot": revised,
               "budget": after,
               "usage_all_evocoder_experiments": {"requests": len(calls),
                    "input_tokens": sum(u["input_tokens"] for u in usage),
                    "output_tokens": sum(u["output_tokens"] for u in usage)},
               "cost_strict_policy_pilot": round(second["budget_after"]["spent_usd"] -
                                                second["budget_before"]["spent_usd"], 12),
               "cost_valid_revision_pilot": round(revised["budget_after"]["spent_usd"] -
                                                 revised["budget_before"]["spent_usd"], 12)}
    output = Path("outputs/evocoder-live-followup")
    output.mkdir(parents=True, exist_ok=True)
    dump(output / "summary.json", summary)
    dump(output / "candidate-v1.json", store.get_artifact(revised["lineage"]["parent"]))
    dump(output / "candidate-v2.json", revised["lineage"])
    dump(output / "evaluation.json", revised["evaluation"])
    dump(output / "protocol-strict.json", second["protocol"])
    dump(output / "protocol-revision.json", revised["protocol"])
    evidence = store.get_artifact(revised["protocol"]["evidence_hash"])
    projection = Evolution.project(evidence)
    for proposal in {call["run_id"] for call in calls if call["run_id"].startswith("proposal-")}:
        events = store.events(proposal)
        if any(e["kind"] == "PROPOSED" and e["payload"]["skill_id"] == revised["skill_id"] for e in events):
            start = next(e for e in events if e["kind"] == "PROPOSAL_START")
            if digest(projection) != start["payload"]["projection_hash"]:
                raise ValueError("exported projection differs from actual proposal input")
            dump(output / "proposal-events.json", events)
            break
    else:
        raise ValueError("actual accepted proposal output missing")
    dump(output / "frozen-evidence.json", evidence)
    dump(output / "model-evidence-projection.json", projection)
    for folder in ["live-002", "live-004"]:
        shutil.copytree(Path("experiments") / folder / "traces", output / "traces" / folder,
                        dirs_exist_ok=True)
    for run_id in revised["protocol"]["evidence_runs"]:
        dump(output / "learning-evidence" / f"{run_id}.json", {"run": store.run(run_id),
                                                               "events": store.events(run_id)})
    shutil.copy2("experiments/live-004/regression-tests.log", output / "tests.log")
    evaluation = revised["evaluation"]
    controls = revised["no_skill_dev_control"]
    phases = Counter(r["state"] for r in second["new_runs"])
    dev_rows = [r for r in evaluation["rows"] if r["split"] == "dev"]
    rows = "\n".join(f"| {r['task_id']} | {r['baseline_pass']} | {r['candidate_pass']} | "
                      f"{next(c['passed'] for c in controls if c['task_id']==r['task_id'])} |" for r in dev_rows)
    probes = "\n".join(f"| {r['task_id']} | {r['arm']} | {r['state']} | {r['steps']} |"
                        for r in revised["reused_probe_matrix"])
    support = []
    for row in evaluation["rows"]:
        if row["split"] != "learn":
            continue
        for arm, key in [("冻结父技能", "baseline_run"), ("修订技能", "candidate_run")]:
            run = store.run(row[key])
            cause = run["result"].get("error") or "无运行异常；通过与否以最终独立验证为准"
            support.append(f"| {row['task_id']} | {arm} | {run['state']} | {html.escape(cause.splitlines()[0])} |")
    report = (
        "# EvoCoder 后续真实模型实验\n\n"
        "本次分开保留严格反馈试验、一次引用失败、扩展反馈规则试验。固定模型权重，"
        "保持每题 16 步预算和同一个 1 USD 累计账本。所有任务为人工编写的缓存合同案例，"
        "不是自然仓库 issue 或 SWE-bench，开发者已知参考实现。\n\n"
        "## 实际技能修订\n\n"
        f"- 父技能：`{revised['lineage']['parent']}`\n"
        f"- 模型修订产物：`{revised['skill_id']}`\n"
        "- 同步学习任务中，模型首次补丁在 invalidate-during-load 测试返回旧值 1，期望值为 2；"
        "随后修复通过。异步任务测试通过后继续改写三次，最终耗尽预算，没有提交补丁。\n"
        "- 扩展后的反馈规则允许从这类已恢复的补丁错误和执行预算耗尽学习，但拒绝将改动前"
        "原始代码的失败当作技能失败。隐藏评分输出不进入提案。\n"
        "- 新技能由真实模型完整生成，未人工修改；原文和逐字段差异见 candidate-v2.json。"
        "规则与接口修改是人工工程工作，不计为智能体自改。\n\n"
        "## 开发集配对结果\n\n| 任务 | 冻结父技能 | 修订技能 | 无技能控制 |\n|---|---|---|---|\n"
        + rows + "\n\n"
        f"核心门禁：{evaluation['gate']['passed']}；父技能 {evaluation['baseline_correct']}/{evaluation['dev_count']}，"
        f"修订技能 {evaluation['candidate_correct']}/{evaluation['dev_count']}；"
        f"无技能控制 {sum(r['passed'] for r in controls)}/{len(controls)}。\n\n"
        f"实际激活：{revised['activated']}；当前 active：`{revised['active'] or '无技能基线'}`。"
        "接纳还要求修订版不退化于无技能控制。没有放宽两个 dev 分组改善、零回归的原有质量门槛。\n\n"
        "## 支持集回放与错误归因\n\n"
        "| 任务 | 方案 | 状态 | 说明 |\n|---|---|---|---|\n" + "\n".join(support) + "\n\n"
        "门禁把 ERROR 保守归入 infrastructure_errors，其中本次实际原因分别是求解命令超时、"
        "模型动作 JSON 不合法，不能将它们描述为外部平台故障。它们也阻止发布，未自动重试或删除。\n\n"
        "## 重复探针\n\n"
        "下列两题已在严格试验运行过，因此是重复探针，不能称为未见终评。"
        "技能版本在探针开始前冻结，结果不写回。\n\n"
        "| 任务 | 方案 | 状态 | 工具步骤 |\n|---|---|---|---|\n" + probes + "\n\n"
        "## 严格规则与失败记录\n\n"
        f"严格试验运行状态：{dict(phases)}。严格规则没有生成新版本，因为最终公开测试均未保持失败，"
        "它忽略了中途错误和预算耗尽。这一负结果保持原样。\n\n"
        "扩展反馈试验的第一次提案引用不属于冻结证据池，被拒绝，费用保留。原始候选正文当时未留存，"
        "不能倒推具体错误引用。随后完善显式引用 ID 枚举并另行冻结协议，只重试一次；"
        "新提案通过引用与父版本校验。当前已将未经语义校验的模型产物也保存为不可变产物。\n\n"
        "收尾审查发现扩展策略仍可能让“仅原始坏代码测试失败、未改写”的运行触发修订。"
        "实验结束后修正了该分支并增加回归测试；已执行的源码快照和协议保持原样，没有按新代码重算成绩。"
        "本轮实际学习证据分别包含改写后失败和步骤耗尽，因此并未依赖该漏洞。\n\n"
        "## 费用与边界\n\n"
        f"- 严格试验：{summary['cost_strict_policy_pilot']:.8f} USD。\n"
        f"- 引用失败尝试：{summary['rejected_reference_attempt']['cost_usd']:.8f} USD。\n"
        f"- 有效修订试验：{summary['cost_valid_revision_pilot']:.8f} USD。\n"
        f"- EvoCoder 所有轮累计：{after['spent_usd']:.8f} USD，预留 {after['reserved_usd']:.8f} USD，上限 1 USD。\n"
        "费用按 API usage 与配置单价估算，不等同供应商账单。小样本、共享缓存概念、"
        "一次配对运行与重复探针都限制结论；没有四组完整实验或统计显著性证明。"
        "候选修订本身已经发生，但本轮没有验证出开发集收益。TTL 重复探针中修订版成功而旧版耗尽预算，"
        "无技能也成功，不能把这一单次现象归因为自进化。\n\n"
        "工程回归：52 项测试通过；Ruff 与 diff 检查通过。所有 315 个 EvoCoder 请求均已结算，"
        "没有 RUNNING 任务或残留执行容器。工程检查不调用付费模型。\n\n"
        "## 下一步实验建议\n\n"
        "暂不继续在这两道探针上筛选版本。下一轮应冻结新的任务和协议，优先接入真实仓库中可复现的"
        "缺陷与回归测试；针对当前异步预算耗尽先分析轨迹、评估技能是否过长或范围过宽。"
        "之后用相同预算比较无记忆、轨迹检索、冻结技能与持续修订，并做配对重复运行。"
        "任何人工调整都另记为工程改动，不能冒充模型自主改进。\n"
    )
    (output / "report.md").write_text(report)
    Path("experiments/live-004/report.md").write_text(report)
    dev_table = table(["任务", "无技能", "冻结 v1", "修订 v2"], [
        [r["task_id"], next(c["passed"] for c in controls if c["task_id"] == r["task_id"]),
         r["baseline_pass"], r["candidate_pass"]] for r in dev_rows])
    probe_table = table(["重复探针", "方案", "状态", "步骤"], [
        [r["task_id"], r["arm"], r["state"], r["steps"]] for r in revised["reused_probe_matrix"]])
    change_blocks = "".join(
        "<details><summary>" + html.escape(field) + "</summary><h4>v1</h4><pre>" +
        html.escape(json.dumps(change["before"], ensure_ascii=False, indent=2)) +
        "</pre><h4>v2</h4><pre>" + html.escape(json.dumps(change["after"], ensure_ascii=False, indent=2)) +
        "</pre></details>" for field, change in revised["lineage"]["changes"].items())
    (output / "report.html").write_text(
        "<!doctype html><html lang='zh-CN'><meta charset='utf-8'><meta name='viewport' "
        "content='width=device-width,initial-scale=1'><title>EvoCoder 技能修订实验</title>"
        "<style>body{font:16px/1.7 system-ui;color:#19303c;max-width:1080px;margin:36px auto;padding:0 24px}"
        "pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#eff5f7;padding:20px;font-size:14px}"
        "h1{line-height:1.3}table{border-collapse:collapse;width:100%;font-size:14px}"
        "td,th{padding:10px;text-align:left;border-bottom:1px solid #ccd7dd}th{background:#eff5f7}"
        ".table{overflow-x:auto}details{margin:12px 0}summary{cursor:pointer;font-weight:600}"
        ".status{padding:20px;border-left:5px solid #b16c20;background:#fff6e9}a{color:#17608c}</style>"
        "<h1>EvoCoder：执行反馈驱动的技能修订</h1>"
        "<p class='status'><strong>真实模型生成了 v1 → v2；开发集没有改善，未接纳、未发布。</strong><br>"
        "模型权重固定。人工修改反馈规则与接口另行记录，不计作自主进化。</p>"
        "<p><a href='candidate-v2.json'>修订技能与差异 JSON</a> · <a href='evaluation.json'>配对评测</a> · "
        "<a href='summary.json'>完整数据</a> · <a href='report.md'>完整报告</a> · "
        "<a href='tests.log'>工程测试</a> · <a href='model-evidence-projection.json'>模型所见证据</a></p>"
        "<h2>修订来自什么反馈</h2><p>同步缓存的首次补丁在失效与加载并发时返回旧值 1，期望值 2；"
        "另一个异步任务在测试通过后继续改写，最终耗尽 16 步预算。模型据此改写了适用条件、"
        "并发失效步骤、验证方法和停止规则，证据与父版本均已绑定。</p>"
        "<h2>开发集</h2><p>三种方案均为 3/4。门禁要求至少两个分组改善、零回归、无运行错误，保持原样。</p>"
        + dev_table + "<h2>技能字段差异</h2>" + change_blocks +
        "<h2>重复探针</h2><p>这些题已执行过，不是未见终评。TTL 案例中修订版和无技能都成功，"
        "旧版耗尽步骤；单次结果不能证明修订收益。</p>" + probe_table +
        "<h2>费用与实验边界</h2><p>本次继续工作新增估算 " +
        f"{after['spent_usd'] - second['budget_before']['spent_usd']:.8f} USD；EvoCoder 累计 " +
        f"{after['spent_usd']:.8f} USD；未结算预留 {after['reserved_usd']:.8f} USD。" +
        "任务为人工编写的缓存案例，非自然仓库 issue；缺少多次重复和完整 RAG 对照。"
        "支持集的命令超时和模型格式错误也保留在报告与轨迹中。</p>"
        "<details><summary>展开完整审计报告</summary><pre>" + html.escape(report) + "</pre></details></html>")
    print(json.dumps({"output": str(output), "skill_id": revised["skill_id"], "gate": evaluation["gate"],
                      "activated": revised["activated"], "budget": after}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
