"""Export the completed real repository pilot without another model call."""
from __future__ import annotations

import argparse
import html
import json
import re
import shutil
from pathlib import Path

from evocoder.contracts import Settings
from evocoder.evolution import Evolution
from evocoder.store import Store


def dump(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def table(headers, rows):
    def cells(values, tag):
        return "".join(f"<{tag}>{html.escape(str(v))}</{tag}>" for v in values)
    return ("<div class='table'><table><tr>" + cells(headers, "th") + "</tr>" +
            "".join("<tr>" + cells(row, "td") + "</tr>" for row in rows) + "</table></div>")


def main(root, output):
    result = json.loads((root / "result.json").read_text())
    settings = Settings()
    store = Store(settings.state_dir)
    protocol = result["protocol"]
    verification = json.loads((root / "verification.json").read_text())
    initial = json.loads(Path("experiments/realworld-001/verification.json").read_text())
    tool_pilot = json.loads(Path("experiments/realworld-002/result.json").read_text())
    stream = result.get("stream", {})
    timeline = stream.get("timeline", [])
    candidate_ids = [r["candidate"] for r in timeline if r.get("candidate")]
    lineage = [r for r in Evolution(settings, store).lineage() if r["id"] in candidate_ids]
    evaluations = [store.get_artifact(r["receipt"]) for r in timeline if r.get("receipt")]
    summary = {"result": result, "verification": verification, "lineage": lineage,
               "evaluations": evaluations,
               "setup_failure": {"root": "experiments/realworld-001", "passed": initial["passed"],
                                 "cause": "Relative PYTHONPATH broke subprocess imports after chdir; fixed with absolute path. Tests preserved.",
                                 "model_calls": 0},
               "tool_pilot": tool_pilot,
               "all_realworld_cost_usd": round(result["budget_after"]["spent_usd"] -
                                                tool_pilot["protocol"]["budget_before"]["spent_usd"], 12),
               "new_cost_usd": round(result["budget_after"]["spent_usd"] - protocol["budget_before"]["spent_usd"], 12)}
    by_arm = {arm: {"passed": sum(r["passed"] for r in result.get("final_matrix", []) if r["arm"] == arm),
                    "count": sum(r["arm"] == arm for r in result.get("final_matrix", []))}
              for arm in ["no_skill", "first_frozen_skill", "latest_skill"]}
    summary["final_by_arm"] = by_arm
    output.mkdir(parents=True, exist_ok=True)
    dump(output / "summary.json", summary)
    dump(output / "protocol.json", protocol)
    dump(output / "verification.json", verification)
    dump(output / "setup-failure.json", initial)
    dump(output / "tool-pilot.json", tool_pilot)
    dump(output / "skills.json", lineage)
    for skill in lineage:
        evidence = store.get_artifact(skill["evidence_hash"])
        dump(output / "evidence" / f"{skill['id']}.json", evidence)
        dump(output / "skills" / f"{skill['id']}.json", skill)
    for name in ["traces", "patches"]:
        if (root / name).exists():
            shutil.copytree(root / name, output / name, dirs_exist_ok=True)
    shutil.copytree("experiments/realworld-002/traces", output / "tool-pilot-traces", dirs_exist_ok=True)
    shutil.copy2("benchmarks/realworld/cases.json", output / "cases.json")
    tests = Path("work/realworld-tests.log").read_text()
    (output / "tests.log").write_text(tests)
    (output / "implementation.md").write_text(Path("docs/realworld.md").read_text())
    learn_rows = [[r["task_id"], r["state"], r.get("candidate", "无新提案"),
                   r.get("gate_passed", "未运行")] for r in timeline]
    dev_rows = [[ev["skill_id"][:12], r["task_id"], r["baseline_pass"], r["candidate_pass"]]
                for ev in evaluations for r in ev["rows"] if r["split"] == "dev"]
    final_rows = [[r["task_id"], r["arm"], r["state"], r["passed"], r["alias_of_existing_run"]]
                  for r in result.get("final_matrix", [])]
    refs = []
    for entry in json.loads((root / "bundle/manifest.json").read_text())["tasks"]:
        metadata = json.loads((root / "bundle/controller" / f"{entry['id']}.json").read_text())["provenance"]
        refs.append({"task_id": entry["id"], "split": entry["split"], "repository": entry["repository"],
                     "pull_request_url": metadata["pull_request_url"],
                     "base_commit": metadata["case"]["base_commit"], "task_hash": metadata["task_hash"]})
    dump(output / "sources.json", refs)
    revised_count = sum(r["parent"] is not None for r in lineage)
    accepted_count = sum(ev["gate"]["passed"] for ev in evaluations)
    dev_text = "；".join(
        f"{ev['skill_id'][:12]}：基线 {ev['baseline_correct']}/{ev['dev_count']}，"
        f"候选 {ev['candidate_correct']}/{ev['dev_count']}，改善分组 {len(ev['improved_groups'])}"
        for ev in evaluations)
    failed_support = sorted({row["task_id"] for ev in evaluations for row in ev["rows"]
                             if row["split"] == "learn" and not row["candidate_pass"]})
    final_text = "，".join(f"{arm} {score['passed']}/{score['count']}" for arm, score in by_arm.items())
    final_errors = []
    for row in result.get("final_matrix", []):
        run = next(r for r in result["new_runs"] if r["id"] == row["run_id"])
        if not row["passed"]:
            final_errors.append(f"{row['task_id']} / {row['arm']}：{row['state']}，"
                                f"{run['result'].get('error') or '预算内未提交或未通过完整独立验证'}")
    conclusion = (f"真实仓库接入已验证；模型产生 {len(lineage)} 个技能候选，其中 {revised_count} 个为父版本修订，"
                  f"{accepted_count} 个通过门禁。当前 active：{result['active'] or '无技能基线'}。")
    conclusion += " 最终测试：" + final_text + "。未验证出稳定技能收益。"
    if not result["completed"]:
        conclusion += "本次任务流未完整完成，不计算完整实验成功率。"
    report = (
        "# EvoCoder 真实仓库缺陷修复探索实验\n\n" + conclusion + "\n\n"
        "任务为 3 个真实 Python 仓库中的 6 个历史 PR 缺陷重建。python-dotenv 用于 learn，"
        "Click 用于 dev，packaging 用于 test，仓库不跨分区。问题摘要与公开复现由开发者整理，"
        "参考源码和上游回归测试仅控制端可见。不是 SWE-bench 官方实验，不保证无预训练污染。\n\n"
        "## 工程与复现\n\n"
        f"最终离线验证：{sum(r['passed'] for r in verification['rows'])}/{len(verification['rows'])}。"
        "每例均检查原始缺陷公开测试失败、原始缺陷上游回归失败、参考补丁两项均通过。"
        "首次环境验证有 2 例失败，原因是相对子进程导入路径；更改为绝对路径后另建 bundle，"
        "没有删除测试或覆盖失败记录，没有为此调用模型。\n\n"
        "随后第一次付费探索（realworld-002）发现搜索工具漏掉超过 100KB 的源码；"
        "模型也出现动作缺少 reason、edit 字段无效等合同错误。该轮中途停止，没有运行 test，"
        "费用和原始记录保留在 tool-pilot.json。修复搜索范围/大小限制并明确工具字段后另行冻结协议，"
        "缺少非执行性 reason 字段时显式标记模型未提供，不伪造解释；非法 edit 仍拒绝。"
        "本轮 dev 是重复使用的开发材料，test 此前未由模型执行；工具改进不能算技能自进化。\n\n"
        "源码工具新增按行读取和精确替换，支持大模块中的小改动。执行镜像按 ID 冻结；"
        "无网络、非 root 容器执行，finish 后独立验证冻结公开测试和上游回归。\n\n"
        "## 学习与技能\n\n" + json.dumps(learn_rows, ensure_ascii=False, indent=2) + "\n\n"
        "技能原文、父版本与证据见 skills.json；提案只读 learn 公开反馈，dev/test 分数不进入提案。"
        "人工工具和环境改动不计作自进化。\n\n"
        "## 开发集配对\n\n" + json.dumps(dev_rows, ensure_ascii=False, indent=2) + "\n\n"
        "每行依次为候选、任务、基线通过、候选通过。门禁保持两个分组改善、零回归、无运行错误。\n\n"
        + dev_text + "。支持集未通过的候选任务：" + str(failed_support) +
        "。这些是单次探索结果，不能写成稳定提升。\n\n"
        "## 冻结最终测试\n\n" + json.dumps(final_rows, ensure_ascii=False, indent=2) + "\n\n"
        "每行依次为任务、方案、状态、通过、是否复用了同版本运行。alias 不是独立重复。"
        "最终测试无经验写回。没有完整轨迹 RAG 对照或多次采样，不能由两题得出泛化结论。\n\n"
        "最终汇总：" + final_text + "。失败明细：\n\n" + "\n\n".join(final_errors) +
        "\n\n整体结果尚不足以证明稳定能力提升，也不能由单次差异证明失败由技能导致。\n\n"
        "## 费用和交付\n\n"
        f"本轮估算 {summary['new_cost_usd']:.8f} USD；EvoCoder 累计 "
        f"{result['budget_after']['spent_usd']:.8f} USD；预留 {result['budget_after']['reserved_usd']:.8f} USD。"
        f"含前次工具探索的真实仓库工作合计 {summary['all_realworld_cost_usd']:.8f} USD。"
        "保持同一个 1 USD 累计账本，费用按 usage 和配置单价估算。\n\n"
        "完整轨迹在 traces/，本地补丁在 patches/；没有向上游创建 PR 或发送消息。"
        "当前入口是冻结源码缺陷修复，自动 CI 触发与依赖迁移尚未接入。\n\n"
        "下一步优先处理无关技能的选择/跳过，以及预算内可恢复的动作参数错误反馈；"
        "不能靠放宽评分或删除失败任务美化结果。增加任务和重复采样前保留本轮 test，"
        "其后再使用时需标为已见开发材料。\n\n"
        "工程测试：\n\n" + tests + "\n"
    )
    (output / "report.md").write_text(report)
    (root / "report.md").write_text(report)
    source_links = "".join("<li>" + html.escape(ref["split"] + " · " + ref["task_id"]) +
                           " · <a href='" + html.escape(ref["pull_request_url"], quote=True) +
                           "'>上游 PR</a></li>" for ref in refs)
    patch_links = "".join(
        "<li><a href='patches/" + html.escape(path.name, quote=True) + "'>" +
        html.escape(next(r["task_id"] + " · " + r["state"] + " · " + r["id"][:12]
                         for r in result["new_runs"] if r["id"] == path.stem)) + "</a></li>"
        for path in sorted((output / "patches").glob("*.patch")))
    (output / "report.html").write_text(
        "<!doctype html><html lang='zh-CN'><meta charset='utf-8'><meta name='viewport' "
        "content='width=device-width,initial-scale=1'><title>EvoCoder 真实仓库实验</title>"
        "<style>body{font:16px/1.7 system-ui;color:#19303c;max-width:1100px;margin:36px auto;padding:0 24px}"
        "h1{line-height:1.3}pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#eff5f7;padding:18px}"
        ".table{overflow-x:auto}table{width:100%;border-collapse:collapse;font-size:14px}"
        "td,th{padding:10px;border-bottom:1px solid #ccd7dd;text-align:left}th{background:#eff5f7}"
        ".status{padding:20px;background:#fff6e9;border-left:5px solid #b16c20}a{color:#17608c}</style>"
        "<h1>EvoCoder：真实仓库缺陷修复</h1><p class='status'>" + html.escape(conclusion) + "</p>"
        "<p><a href='summary.json'>完整数据</a> · <a href='skills.json'>技能版本</a> · "
        "<a href='verification.json'>离线复现</a> · <a href='tests.log'>工程测试</a> · "
        "<a href='implementation.md'>运行说明</a> · <a href='report.md'>完整报告</a></p>"
        "<h2>来源与隔离</h2><p>3 个仓库、6 个上游 PR，按仓库隔离 learn/dev/test。"
        "人工整理的历史缺陷重建，不能称为官方 SWE-bench 或无污染测试。</p><ul>" + source_links + "</ul>"
        "<h2>学习过程</h2>" + table(["任务", "状态", "候选", "门禁"], learn_rows) +
        "<h2>开发集</h2>" + table(["候选", "任务", "基线", "候选"], dev_rows) +
        "<h2>冻结最终测试</h2>" + table(["任务", "方案", "状态", "通过", "复用同一运行"], final_rows) +
        "<details><summary>本地审查补丁（包含失败结果，按状态区分）</summary><ul>" + patch_links + "</ul></details>"
        "<h2>费用与边界</h2><p>本轮估算 " + f"{summary['new_cost_usd']:.8f}" +
        " USD。缺少 RAG 完整对照、更多仓库和重复采样；机制运行不等于已验证能力提升。</p>"
        "<details><summary>展开完整审计报告</summary><pre>" + html.escape(report) + "</pre></details></html>")
    if settings.api_key:
        for path in output.rglob("*"):
            if path.is_file() and settings.api_key in path.read_text():
                raise ValueError("credential found in report export")
    print(json.dumps({"output": str(output), "conclusion": conclusion,
                      "new_cost_usd": summary["new_cost_usd"],
                      "tests": re.findall(r"\d+ passed[^\n]*", tests)}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path("experiments/realworld-003"))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    main(args.root, args.output)
