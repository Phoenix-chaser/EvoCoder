from __future__ import annotations

import html
import json
from pathlib import Path

from evocoder.contracts import Settings, canonical, digest
from evocoder.dataset import create_fixtures, load_catalog
from evocoder.evolution import Evolution
from evocoder.runtime import Runner
from evocoder.store import Store, uid


def export_report(root: Path, report: dict):
    root.mkdir(parents=True, exist_ok=True)
    (root / "summary.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    rows = "\n".join(f"<tr><td>{html.escape(r['task_id'])}</td><td>{r['baseline_pass']}</td>"
                     f"<td>{r['candidate_pass']}</td></tr>" for r in report["evaluation"]["rows"])
    changes = json.dumps(report["lineage"], ensure_ascii=False, indent=2)
    page = ("<!doctype html><html lang='zh-CN'><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width,initial-scale=1'>"
            "<title>EvoCoder 技能修订报告</title><style>body{font:16px/1.6 system-ui;max-width:1000px;"
            "margin:40px auto;padding:0 24px;color:#182c3c}pre{white-space:pre-wrap;overflow-wrap:anywhere;"
            "background:#edf3f6;padding:20px}table{border-collapse:collapse}td,th{padding:8px 20px;"
            "border:1px solid #ccd8df}</style><h1>EvoCoder 技能持续修订</h1>"
            "<p><strong>Mock 工程流程验证。合成任务、脚本化模型，不代表真实模型学习效果。</strong></p>"
            "<p>历史执行 → 技能 v1 候选 → 后续公开失败 → 技能 v2 → 对照回放 → 发布 → 新任务调用</p>"
            f"<p>v1: <code>{report['v1'][:16]}</code> / v2: <code>{report['v2'][:16]}</code></p>"
            "<h2>开发与支持集逐例对照</h2><table><tr><th>任务</th><th>父技能</th><th>新技能</th></tr>"
            + rows + "</table><h2>版本、证据与实际修改</h2><pre>" + html.escape(changes)
            + "</pre></html>")
    (root / "report.html").write_text(page)
    text = ("# EvoCoder 核心闭环验证\n\n"
            "本报告来自 Mock 模型和合成工程任务，不能用作真实模型性能提升结论。\n\n"
            f"- 初始学习运行：`{report['initial_run']}`\n"
            f"- v1 候选：`{report['v1']}`\n"
            f"- v1 试用失败运行：`{report['failure_run']}`\n"
            f"- v2 修订：`{report['v2']}`\n"
            f"- 评测收据：`{report['evaluation']['receipt']}`\n"
            f"- 发布版本：`{report['active']}`\n"
            f"- 冻结终评任务数：{len(report['test_runs'])}\n\n"
            "测试阶段没有写回经验。每例均有独立工作区、源码补丁、工具反馈与评分。\n"
            "完整版本差异、逐例结果和费用见 summary.json，浏览报告见 report.html。\n")
    (root / "report.md").write_text(text)


def demo(settings: Settings, output: Path | None = None) -> dict:
    if settings.mode != "mock":
        raise ValueError("demo is explicitly offline; use run/propose/evaluate for authorized live work")
    directory = (output or Path("experiments") / ("demo-" + uid()[:10])).resolve()
    if (directory / "summary.json").exists():
        raise ValueError("demo output exists; choose a new directory to preserve results")
    directory.mkdir(parents=True, exist_ok=True)
    catalog = directory / "dataset"
    manifest = create_fixtures(catalog)
    tasks = load_catalog(catalog / "manifest.json")
    local = settings.model_copy(update={"state_dir": directory / "state"})
    store = Store(local.state_dir)
    learn = [t for t in tasks if t.split == "learn"]
    runner = Runner(local, store)
    evolution = Evolution(local, store, grounding_tasks={t.id: t for t in learn})
    initial = runner.solve(learn[0])
    if not store.run(initial)["result"]["passed"]:
        raise RuntimeError("initial fixture failed; inspect stored trace")
    v1 = evolution.propose([initial])
    failed = runner.solve(learn[1], v1, use_active=False, candidate=True)
    if not store.run(failed)["result"]["public_failed"]:
        raise RuntimeError("revision fixture did not provide public failure evidence")
    v2 = evolution.propose([initial, failed], v1)
    evaluation = evolution.evaluate(v2, [t for t in tasks if t.split == "dev"], [learn[0]])
    if not evaluation["gate"]["passed"]:
        raise RuntimeError("candidate rejected; baseline retained; inspect stored evaluator receipt")
    store.activate(v2, evaluation["receipt"], expected=None)
    before = len(evolution.lineage())
    test_runs = [runner.solve(t) for t in tasks if t.split == "test"]
    assert before == len(evolution.lineage())
    report = {"schema": 1, "mode": "mock", "claim": "engineering_validation_only",
              "dataset_hash": manifest["manifest_hash"], "settings": local.public(),
              "initial_run": initial, "failure_run": failed, "v1": v1, "v2": v2,
              "evaluation": evaluation, "active": store.active(),
              "lineage": evolution.lineage(), "test_runs": [store.run(r) for r in test_runs],
              "budget": store.budget(), "output": str(directory)}
    # Include actual tool traces rather than relying on an aggregate success number.
    all_runs = [initial, failed, *test_runs]
    for row in evaluation["rows"]:
        all_runs.extend([row["baseline_run"], row["candidate_run"]])
    trace_dir = directory / "traces"
    trace_dir.mkdir()
    for run_id in all_runs:
        (trace_dir / (run_id + ".json")).write_text(canonical(
            {"run": store.run(run_id), "events": store.events(run_id)}) + "\n")
    report["report_hash"] = digest(report)
    export_report(directory, report)
    return report
