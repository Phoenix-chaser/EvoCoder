"""Export actual paid-pilot artifacts without including credentials or hidden tests."""
from __future__ import annotations

import html
import json
import shutil
from pathlib import Path

from evocoder.contracts import Settings
from evocoder.evolution import Evolution
from evocoder.store import Store


def main():
    root = Path("experiments/live-001")
    settings = Settings()
    store = Store(settings.state_dir)
    source = json.loads((root / "pilot-report/summary.json").read_text())
    with store.connect() as db:
        run_ids = [r[0] for r in db.execute("SELECT id FROM runs ORDER BY created")]
        calls = [dict(r) for r in db.execute("SELECT * FROM calls ORDER BY rowid")]
        proposal_events = [dict(r) for r in db.execute(
            "SELECT run_id,kind,payload FROM events WHERE run_id LIKE 'proposal-%' ORDER BY rowid")]
        evaluations = [{"receipt": r[0], **json.loads(r[1])} for r in db.execute(
            "SELECT id,report FROM evaluations ORDER BY rowid")]
    runs = [store.run(r) for r in run_ids]
    usage = [json.loads(c["usage"]) for c in calls if c["usage"]]
    budget = store.budget()
    summary = {"schema": 1, "mode": "live", "claim": "exploratory_local_reused_tasks",
               "model": settings.model, "protocol": json.loads((root / "protocol.json").read_text()),
               "runtime_source": json.loads((root / "source-fingerprint.json").read_text()),
               "runs": runs, "timeline": source["timeline"], "lineage": Evolution(settings, store).lineage(),
               "evaluations": evaluations, "final_runs": source["final_runs"],
               "active": store.active(), "budget": budget,
               "provider_usage": {"requests": len(calls),
                                  "settled_requests": sum(c["state"] == "settled" for c in calls),
                                  "input_tokens": sum(u["input_tokens"] for u in usage),
                                  "output_tokens": sum(u["output_tokens"] for u in usage)},
               "proposal_events": proposal_events, "final_test_learning": False}
    output = Path("outputs/evocoder-live-001")
    output.mkdir(parents=True, exist_ok=True)
    text_json = json.dumps(summary, ensure_ascii=False, indent=2) + "\n"
    if settings.api_key and settings.api_key in text_json:
        raise RuntimeError("credential found in export")
    (root / "summary.json").write_text(text_json)
    (output / "summary.json").write_text(text_json)
    shutil.copy2(root / "protocol.json", output / "protocol.json")
    shutil.copy2(root / "regression-tests.log", output / "tests.log")
    traces = output / "traces"
    traces.mkdir(exist_ok=True)
    for run_id in run_ids:
        document = json.dumps({"run": store.run(run_id), "events": store.events(run_id)},
                              ensure_ascii=False, indent=2) + "\n"
        if settings.api_key and settings.api_key in document:
            raise RuntimeError("credential found in trace")
        (traces / f"{run_id}.json").write_text(document)
    for i, item in enumerate(summary["lineage"], 1):
        (output / f"candidate-{i:02d}.json").write_text(json.dumps(item, ensure_ascii=False, indent=2) + "\n")
    gates = []
    for e in evaluations:
        gates.append(f"| {e['skill_id'][:12]} | {e['baseline_correct']}/{e['dev_count']} | "
                     f"{e['candidate_correct']}/{e['dev_count']} | {e['gate']['passed']} |")
    final = "\n".join(f"| {r['task_id']} | {r['state']} | {r['skill_id'] or '无技能基线'} |"
                      for r in source["final_runs"])
    revised = sum(r["parent"] is not None for r in summary["lineage"])
    report = (
        "# EvoCoder 首轮真实模型探索实验\n\n"
        f"模型：`{settings.model}`。固定模型权重。8 道本地已有自建较难任务，learn 4 / dev 2 / test 2。\n\n"
        "这些任务复用自 CodeAgent-R，开发者已知其内容；不属于自然 GitHub issue、SWE-bench 成绩或未见独立终评。\n\n"
        f"本轮生成 {len(summary['lineage'])} 个候选，其中 {revised} 个有父版本修订；"
        f"当前接纳版本：`{store.active() or '无，保留无技能基线'}`。\n\n"
        "## 开发集门禁\n\n| 候选 | 基线 | 候选 | 通过 |\n|---|---|---|---|\n"
        + "\n".join(gates) + "\n\n## 冻结后任务\n\n| 任务 | 状态 | 实际技能 |\n|---|---|---|\n"
        + final + "\n\n## 费用与失败保留\n\n"
        f"- 本轮累计估算费用：{budget['spent_usd']:.8f} USD；未结算预留：{budget['reserved_usd']:.8f} USD；上限 1 USD。\n"
        f"- API 请求：{len(calls)}；输入 tokens：{summary['provider_usage']['input_tokens']}；"
        f"输出 tokens：{summary['provider_usage']['output_tokens']}。\n"
        "- 费用按提供商 usage 与既有配置单价计算，不等同于供应商账单，未单独处理缓存阶梯定价。\n"
        "- 首次连通运行因函数调用合同不匹配停止，其已计费记录仍保留。后续人工明确动作协议并增加安全诊断，重跑完成；该修改属于工程修复，不能算系统自进化。\n\n"
        "## 结论边界\n\n"
        "候选内容由真实模型生成，未手工改写。门禁未放宽，终评没有写回经验。"
        "若没有候选通过或没有发生修订，就没有证据宣称本轮实现了有效的持续学习。"
        "单轮小样本结果不足以支持泛化或统计结论；四组对照与自然任务实验仍待完成。\n\n"
        "接口参考：[DeepSeek Chat Completions 官方合同](https://api-docs.deepseek.com/api/create-chat-completion/)。\n"
    )
    (root / "report.md").write_text(report)
    (output / "report.md").write_text(report)
    (output / "report.html").write_text(
        "<!doctype html><html lang='zh-CN'><meta charset='utf-8'><meta name='viewport' "
        "content='width=device-width,initial-scale=1'><title>EvoCoder 真实实验</title>"
        "<style>body{font:16px/1.7 system-ui;max-width:1000px;margin:40px auto;padding:0 24px}"
        "pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#f0f5f7;padding:24px}</style>"
        "<h1>EvoCoder 首轮真实模型实验</h1><p><a href='summary.json'>完整数据</a> · "
        "<a href='report.md'>Markdown 报告</a></p><pre>" + html.escape(report) + "</pre></html>")
    print(json.dumps({"output": str(output), "budget": budget, "proposals": len(summary["lineage"]),
                      "revisions": revised, "active": store.active(), "requests": len(calls)}, indent=2))


if __name__ == "__main__":
    main()
