"""Read-only report export. No model calls, state writes or experience updates."""

import argparse
import html
import json
import shutil
import sqlite3
from collections import Counter
from pathlib import Path

from evocoder.contracts import Settings, canonical, digest
from evocoder.statistics import summarize


def read_state(path):
    if not path.is_file():
        return None
    db = sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    return db


def table(headers, rows):
    def cells(values, tag):
        return "".join(f"<{tag}>{html.escape(str(v))}</{tag}>" for v in values)

    return (
        '<div class="scroll"><table><tr>'
        + cells(headers, "th")
        + "</tr>"
        + "".join("<tr>" + cells(r, "td") + "</tr>" for r in rows)
        + "</table></div>"
    )


def export(root, output):
    protocol = json.loads((root / "protocol.json").read_text())
    result = (
        json.loads((root / "result.json").read_text())
        if (root / "result.json").exists()
        else {"completed": False, "running": True}
    )
    manifest = json.loads(Path(protocol["manifest"]).read_text())
    tasks = [t["id"] for t in manifest["tasks"] if t["split"] == "test"]
    rows = []
    for path in sorted((root / "cells").glob("*.json")):
        row = json.loads(path.read_text())
        if row["state"] != "started":
            rows.append(row)
    stats = summarize(rows, tasks, protocol["repeats"])
    output.mkdir(parents=True, exist_ok=True)
    shutil.copytree(root / "source", output / "source", dirs_exist_ok=True)
    shutil.copy2(root / "protocol.json", output / "protocol.json")
    shutil.copy2(Path(protocol["manifest"]), output / "manifest.json")
    shutil.copy2(Path(protocol["verification"]), output / "offline-verification.json")
    method = root / "method-at-start.md"
    shutil.copy2(
        method if method.exists() else Path("docs/comparison-protocol.md"), output / "implementation.md"
    )
    test_log = (
        "work/comparison-tests-v4-final.log"
        if protocol.get("continuation")
        else "work/comparison-tests-v3-final.log"
        if protocol.get("seed_from")
        else "work/comparison-tests-v2-final.log"
        if protocol["settings"].get("cached_input_price_per_million")
        else "work/comparison-tests.log"
    )
    shutil.copy2(test_log, output / "tests.log")
    data = output / "data"
    data.mkdir(exist_ok=True)
    for name in [
        "acquisition.json",
        "experience-pool.json",
        "seed-reports.json",
        "frozen-test-state.json",
        "result.json",
    ]:
        if (root / name).exists():
            shutil.copy2(root / name, data / name)
    for name in ["learning-C", "learning-D", "cells"]:
        if (root / name).exists():
            shutil.copytree(root / name, data / name, dirs_exist_ok=True)
    previous_costs = {}
    pending = [protocol.get("history_from"), protocol.get("seed_from")]
    provenance = output / "provenance"
    while pending:
        source = pending.pop()
        if not source:
            continue
        previous = Path(source).parent
        key = str(previous.resolve())
        if key in previous_costs or previous.resolve() == root.resolve():
            continue
        if not (previous / "result.json").exists():
            continue
        prior = json.loads((previous / "result.json").read_text())
        previous_costs[key] = prior["new_cost_usd"]
        dest = provenance / previous.name
        dest.mkdir(parents=True, exist_ok=True)
        shutil.copy2(previous / "result.json", dest / "result.json")
        shutil.copy2(previous / "protocol.json", dest / "protocol.json")
        shutil.copytree(previous / "source", dest / "source", dirs_exist_ok=True)
        pending.extend([prior["protocol"].get("history_from"), prior["protocol"].get("seed_from")])
    lineage = {}
    counts = {}
    provider = set()
    fingerprints = set()
    applications = Counter()
    traces = output / "traces"
    traces.mkdir(exist_ok=True)
    for arm, folder in [("common", "common"), ("seed", "seed"), ("B", "B"), ("C", "C"), ("D", "D")]:
        db = read_state(root / folder / "evocoder.sqlite3")
        if db is None:
            continue
        skill_rows = [dict(r) for r in db.execute("SELECT * FROM skills ORDER BY rowid")]
        for row in skill_rows:
            body = json.loads(db.execute("SELECT body FROM artifacts WHERE id=?", (row["id"],)).fetchone()[0])
            assert digest(body) == row["id"]
            row["skill"] = body
            evidence = json.loads(
                db.execute("SELECT body FROM artifacts WHERE id=?", (row["evidence_hash"],)).fetchone()[0]
            )
            assert digest(evidence) == row["evidence_hash"]
            row["evidence"] = evidence
        lineage[arm] = skill_rows
        run_rows = [dict(r) for r in db.execute("SELECT * FROM runs ORDER BY created")]
        counts[arm] = dict(Counter(r["state"] for r in run_rows))
        arm_traces = traces / arm
        arm_traces.mkdir(exist_ok=True)
        for run in run_rows:
            run["result"] = json.loads(run["result"]) if run["result"] else None
            run["config"] = json.loads(run["config"])
            events = [
                {"seq": r[0], "kind": r[1], "payload": json.loads(r[2])}
                for r in db.execute(
                    "SELECT seq,kind,payload FROM events WHERE run_id=? ORDER BY seq", (run["id"],)
                )
            ]
            for event in events:
                if event["kind"] == "MODEL_RESPONSE":
                    provider.add(event["payload"].get("provider_model", ""))
                    fingerprints.add(event["payload"].get("system_fingerprint", ""))
            if run["split"] == "test" and run["purpose"] == "evaluate" and run["skill_id"]:
                applications[arm] += 1
            (arm_traces / f"{run['id']}.json").write_text(canonical({"run": run, "events": events}) + "\n")
        for row in rows:
            key = "common" if row["arm"] == "A" else row["arm"]
            if key == arm:
                actual = db.execute(
                    "SELECT result,task_id,split,purpose FROM runs WHERE id=?", (row["run_id"],)
                ).fetchone()
                assert (
                    actual and actual[1] == row["task_id"] and actual[2] == "test" and actual[3] == "evaluate"
                )
                assert json.loads(actual[0])["passed"] == row["passed"]
        db.close()
    ledger = read_state(Path(protocol["settings"]["state_dir"]) / "evocoder.sqlite3")
    if result.get("budget_after"):
        frozen_budget = result["budget_after"]
        budget = {
            "cap": frozen_budget["cap_usd"],
            "spent": frozen_budget["spent_usd"],
            "reserved": frozen_budget["reserved_usd"],
            "held": frozen_budget.get("held_usd", 0),
        }
    else:
        raw = dict(ledger.execute("SELECT * FROM budget").fetchone())
        budget = {k: raw[k] / 1e9 for k in ["cap", "spent", "reserved"]}
        budget["held"] = (
            ledger.execute("SELECT COALESCE(SUM(amount),0) FROM budget_holds").fetchone()[0] / 1e9
        )
    ledger.close()
    delta = round(budget["spent"] - protocol["budget_before"]["spent_usd"], 12)
    revised_ids = {r["id"] for r in lineage.get("D", []) if r["parent"]}
    revised_applications = [r for r in rows if r["arm"] == "D" and r["applied_skill_id"] in revised_ids]
    summary = {
        "protocol": protocol,
        "result": result,
        "statistics": stats,
        "rows": rows,
        "lineage": lineage,
        "run_states": counts,
        "provider_models": sorted(provider - {""}),
        "system_fingerprints": sorted(fingerprints - {"", "None"}),
        "actual_skill_applications": dict(applications),
        "revised_skill_test_applications": len(revised_applications),
        "supports_revision_benefit": bool(stats["supports_positive_primary_effect"] and revised_applications),
        "budget": budget,
        "new_cost_usd": delta,
        "previous_preparation_costs_usd": previous_costs,
        "cost_including_reused_preparation_usd": delta + sum(previous_costs.values()),
        "source_snapshot_hash_matches": digest(
            {p.name: p.read_text() for p in (root / "source").glob("*.py")}
        )
        == protocol["source_hash"],
    }
    stop_audit = (
        json.loads((root / "stop-audit.json").read_text()) if (root / "stop-audit.json").exists() else None
    )
    summary["stop_audit"] = None
    if stop_audit:
        shareable = {k: stop_audit[k] for k in (
            "stop_code", "phase", "completed", "test_cells", "retained_test_cells", "source_hash_matches",
            "manifest_hash_matches", "verification_hash_matches", "next_model",
        ) if k in stop_audit}
        summary["stop_audit"] = shareable
        (data / "stop-audit.json").write_text(json.dumps(shareable, ensure_ascii=False, indent=2) + "\n")
    stop_note = ""
    if stop_audit and stop_audit.get("stop_code") == "model_http_402":
        stop_note = "供应商返回 HTTP 402，运行器停止派发。续跑前须核对未结算请求并从已审计的检查点恢复。"
    elif stop_audit and stop_audit.get("stop_code") == "user_requested_model_switch":
        stop_note = (
            "切换 GLM 前，本轮已停止派发 DeepSeek 任务，所有在途请求已正常结束。"
            "保留 157/384 次终评结果；不补造剩余成绩，不与 GLM 结果混合，不据此宣称收益。"
        )
    (output / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
    score_rows = []
    for arm, score in stats["scores"].items():
        rate = score["success_rate_observed"]
        score_rows.append(
            [
                arm,
                protocol["arms"][arm],
                f"{score['passed']}/{score['completed_cells']}",
                f"{100 * rate:.1f}%" if rate is not None else "未运行",
                f"{score['cost_usd']:.5f}",
                score["execution_errors"],
            ]
        )
    primary = stats["contrasts"]["D_vs_C"]
    conclusion = (
        "终评矩阵尚未完成，不能判断收益"
        if not stats["complete"]
        else "终评未实际使用修订技能，不能归因于持续修订"
        if not revised_applications
        else "本次冻结分布下观察到正向修订收益证据"
        if summary["supports_revision_benefit"]
        else "完整对照未证实修订收益"
    )
    status = "已完成" if result.get("completed") else "运行中" if result.get("running") else "已停止／未完成"
    text = (
        "# EvoCoder 真实模型四组实验\n\n"
        f"状态：{status}。{conclusion}。\n\n"
        + (stop_note + "\n\n" if stop_note else "")
        + f"终评完成 {stats['observed_cells']}/{stats['expected_cells']} 格，完整配对任务 {stats['paired_tasks']}/32。"
        "每题三次采样不等于三个独立任务。\n\n"
        "## 四组结果\n\n" + json.dumps(stats["scores"], ensure_ascii=False, indent=2) + "\n\n"
        "## 主要比较 D−C\n\n" + json.dumps(primary, ensure_ascii=False, indent=2) + "\n\n"
        "## 费用与学习\n\n"
        f"本轮估算 {delta:.8f} USD；累计 {budget['spent']:.8f} USD；预留 {budget['reserved']:.8f} USD；"
        f"累计上限 {budget['cap']:.2f} USD。\n\n"
        f"计入被复用的历史准备与失败预跑，合计估算 {summary['cost_including_reused_preparation_usd']:.8f} USD。\n\n"
        f"另保留历史价格差额 allowance {budget['held']:.8f} USD；这不是模型费用。\n\n"
        f"D 中父版本非空的技能版本数：{sum(bool(r['parent']) for r in lineage.get('D', []))}。"
        f"终评实际应用修订技能 {len(revised_applications)} 次。"
        f"终评实际技能应用：{dict(applications)}。共同历史/初始技能与后续学习费用见阶段记录，"
        "表中费用仅为终评调用，不能用它单独声称端到端更省钱。\n\n"
        f"阶段：{result.get('phase', '参见 data/ 中进度')}；错误：{result.get('error', '无')}。\n\n"
        "## 结论边界\n\n"
        "80 例来自 7 个真实仓库的历史修复提交，描述取自提交说明，按仓库内时间和不同修改函数分区。"
        "不是官方 SWE-bench，不保证无预训练污染，也不是跨仓库泛化实验。只进行一个学习顺序，"
        "三个测试重复不是三个独立进化过程。RAG 使用固定的简单文本检索，不代表最强 RAG。\n\n"
        "主要结论需要完整矩阵、至少 20 个配对缺陷组和 D−C 的 95% bootstrap 区间下界大于零；"
        "区间跨零或没有真正产生修订时，应分别报告证据不足或未触发修订。保留所有失败与退化，"
        "不因结果不理想重新挑选测试题。API 模型别名和返回标识已记录，不能独立验证服务端权重未变。\n"
    )
    (output / "report.md").write_text(text)
    (output / "report.html").write_text(
        '<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
        "<title>EvoCoder 四组真实实验</title><style>body{font:16px/1.7 system-ui;max-width:1120px;margin:32px auto;padding:0 24px;color:#19303c}"
        "table{border-collapse:collapse;width:100%;font-size:14px}td,th{padding:10px;border-bottom:1px solid #d1dce2;text-align:left}"
        ".scroll{overflow:auto}.note{padding:20px;background:#fff5df}pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#eff5f7;padding:20px}"
        'a{color:#17608c}</style><h1>EvoCoder：四组真实模型对照</h1><p class="note">'
        + html.escape(status + "；" + conclusion)
        + f"<br>已记录 {stats['observed_cells']}/{stats['expected_cells']} 个终评运行。</p>"
        + ('<p class="note">' + html.escape(stop_note) + "</p>" if stop_note else "")
        + '<p><a href="summary.json">完整数据与演化证据</a> · <a href="protocol.json">冻结协议</a> · <a href="manifest.json">80 例清单</a> · '
        '<a href="offline-verification.json">离线复现</a> · <a href="implementation.md">实验方法</a> · <a href="tests.log">工程测试</a> · '
        '<a href="report.md">完整报告</a></p>'
        + table(["组", "方案", "通过/已完成", "观察通过率", "终评 USD", "执行错误"], score_rows)
        + "<h2>详细说明</h2><pre>"
        + html.escape(text)
        + "</pre></html>"
    )
    key = Settings().api_key
    for path in output.rglob("*"):
        if path.is_file() and key and key.encode() in path.read_bytes():
            raise ValueError("credential found in export")
    print(
        {"status": status, "cells": stats["observed_cells"], "new_cost_usd": delta, "conclusion": conclusion}
    )


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--root", type=Path, default=Path("experiments/comparison-live-004"))
    p.add_argument("--output", type=Path, required=True)
    args = p.parse_args()
    export(args.root, args.output)
