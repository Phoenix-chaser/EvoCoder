from __future__ import annotations

import argparse
import html
import json
import shutil
from pathlib import Path

from evocoder.contracts import Settings


def table(headers, rows):
    def cells(values, tag):
        return "".join(f"<{tag}>{html.escape(str(value))}</{tag}>" for value in values)
    return ("<div class='table'><table><tr>" + cells(headers, "th") + "</tr>" +
            "".join("<tr>" + cells(row, "td") + "</tr>" for row in rows) + "</table></div>")


def main(root, output):
    result = json.loads((root / "result.json").read_text())
    protocol = result["protocol"]
    scores = []
    for arm in protocol["arms"]:
        rows = [r for r in result["rows"] if r["arm"] == arm]
        scores.append({"arm": arm, "passed": sum(r["passed"] for r in rows), "total": len(rows),
                       "rejections": sum(r["rejected_actions"] for r in rows),
                       "model_steps": sum(r["model_steps"] for r in rows),
                       "cost_usd": round(sum(r["cost"]["cost_usd"] for r in rows), 12),
                       "applied": sum(r["applied_skill_id"] is not None for r in rows)})
    routed = [r for r in result["rows"] if r["arm"].startswith("route_")]
    skips = sum(r["routing"]["decision"] == "skip" for r in routed)
    invalid = sum(r["routing"]["decision"] == "skip_invalid" for r in routed)
    probe = result.get("positive_probe", {})
    probe_applied = bool(probe.get("result", {}).get("applied_skill_id"))
    delta = round(result["budget_after"]["spent_usd"] - protocol["budget_before"]["spent_usd"], 12)
    summary = {"result": result, "scores": scores, "routing_skips": skips,
               "invalid_routing": invalid, "positive_probe_applied": probe_applied, "new_cost_usd": delta}
    output.mkdir(parents=True, exist_ok=True)
    (output / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
    shutil.copy2(root / "protocol.json", output / "protocol.json")
    shutil.copy2("work/optimization-tests.log", output / "tests.log")
    shutil.copy2("docs/routing-recovery.md", output / "implementation.md")
    for name in ["traces", "patches", "rejected-arguments"]:
        if (root / name).exists():
            shutil.copytree(root / name, output / name, dirs_exist_ok=True)
    display = [[s["arm"], f"{s['passed']}/{s['total']}", s["applied"], s["rejections"], s["model_steps"],
                f"{s['cost_usd']:.8f}"] for s in scores]
    row_display = [[r["task_id"], r["arm"], r["state"], r["routing"]["decision"], r["model_steps"],
                    r["rejected_actions"]] for r in result["rows"]]
    tests = Path("work/optimization-tests.log").read_text()
    report = (
        "# EvoCoder：技能适用判断与动作纠错\n\n"
        "本轮是人工实现的执行工程优化，不是新的技能自进化实验。固定已有修订技能，"
        "没有生成或修改技能、发布版本或重跑旧终评。实验只用两道已见 Click 开发案例，"
        "另用已见的 dotenv 空缓存案例核对正向适用。\n\n"
        "## 实现\n\n"
        "- 路由只读取公开问题、技能适用条件和禁用条件，不读取修复步骤、历史分数或隐藏测试。"
        "应用需有合法条件索引、问题原文引用且无阻断条件；skip/uncertain/非法路由均显式跳过技能。"
        "这仍是模型判断，原文引用只能证明有出处，不能证明语义判断一定正确。\n"
        "- 记录 requested_skill_id 与实际 applied_skill_id；跳过技能的失败不能被当作父版本失败来修订。\n"
        "- 完整且已结算的动作参数校验失败会返回反馈，不执行动作、不裁剪参数；模型可在剩余预算内纠正。"
        "网络超时、未知函数和不确定计费仍终止，不自动重发 HTTP。\n"
        "- 原来的 16 步上限保持不变，路由与被拒绝的动作均占模型步骤；工具次数另外统计。\n\n"
        "## 受控测试\n\n" + tests + "\n"
        "其中 MockTransport 与 Docker 共同覆盖 read(240) 被拒绝、随后成功提交；连续无效动作耗尽预算；"
        "关闭恢复时首次参数错误终止；跳过/应用技能的上下文与父版本证据绑定；路由超时不重试且保留预留。"
        "这些为工程测试，不是付费模型能力提升成绩。\n\n"
        "## 固定开发案例对照\n\n"
        "force_stop=直接注入技能/参数错误终止，force_recover=直接注入/允许纠正，"
        "route_stop=先判断适用/参数错误终止，route_recover=两项开启，no_skill_recover=无技能控制。"
        "五组使用同一当前运行器、同一技能和任务、相同总模型步骤；不是旧二进制与新版本的历史成绩比较。\n\n"
        + json.dumps(scores, ensure_ascii=False, indent=2) + "\n\n"
        f"路由分组中明确 skip {skips}/{len(routed)}；非法路由 {invalid}；正向缓存探针实际应用技能："
        f"{probe_applied}，最终状态 {probe.get('state', '未运行')}。\n\n"
        "每个组合只有一次运行，且任务已经用于开发，不能作为未见泛化或统计提升证据。"
        "尤其当两个分组均没有发生参数拒绝时，它们的通过率差异不能归因于纠错开关；"
        "同模型与 temperature=0 也不保证完全相同的输出。\n\n"
        "## 费用与发布\n\n"
        f"本轮估算 {delta:.8f} USD；EvoCoder 累计 {result['budget_after']['spent_usd']:.8f} USD，"
        f"预留 {result['budget_after']['reserved_usd']:.8f} USD，上限仍为 1 USD。"
        f"技能变更：{result['skill_mutation']}；发布变更：{result['release_mutation']}；active：{result['active']}。\n\n"
        "仍为单个候选技能的适用筛选，没有实现多技能检索排序。动作格式之外的 HTTP 协议/传输错误、"
        "命令超时和越权写入仍不能通过这套纠错机制绕过。通过测试不代表生产或任意仓库正确性保证。\n"
    )
    (output / "report.md").write_text(report)
    (root / "report.md").write_text(report)
    title = f"路由明确跳过 {skips}/{len(routed)}；正向探针应用 {probe_applied}；技能未发布"
    (output / "report.html").write_text(
        "<!doctype html><html lang='zh-CN'><meta charset='utf-8'><meta name='viewport' "
        "content='width=device-width,initial-scale=1'><title>EvoCoder 适用判断与纠错</title>"
        "<style>body{font:16px/1.7 system-ui;max-width:1100px;color:#19303c;margin:36px auto;padding:0 24px}"
        "table{width:100%;border-collapse:collapse;font-size:14px}th,td{padding:10px;border-bottom:1px solid #ccd7dd;"
        "text-align:left}th,pre{background:#eff5f7}.table{overflow-x:auto}pre{white-space:pre-wrap;"
        "overflow-wrap:anywhere;padding:20px}.status{background:#fff6e9;padding:20px;border-left:5px solid #b16c20}"
        "a{color:#17608c}</style><h1>EvoCoder：技能适用判断与动作纠错</h1><p class='status'>" + html.escape(title) +
        "<br>人工工程优化；两道已见开发题的单次对照，不能解释成自进化性能提升。</p>"
        "<p><a href='summary.json'>完整数据</a> · <a href='protocol.json'>冻结协议</a> · "
        "<a href='tests.log'>工程测试</a> · <a href='implementation.md'>使用说明</a> · <a href='report.md'>完整报告</a></p>"
        "<h2>固定开发案例</h2>" + table(["方案", "通过", "实际注入", "参数拒绝", "模型步骤", "USD"], display) +
        "<h2>逐次运行</h2>" + table(["任务", "方案", "状态", "路由", "模型步骤", "参数拒绝"], row_display) +
        "<h2>结论边界</h2><p>路由和被拒绝的动作均计入 16 步。没有触发参数拒绝的运行，"
        "不能用于证明纠错的因果收益；受控测试另行报告。无技能控制保持相同工具、模型与预算。</p>"
        "<details><summary>完整审计报告</summary><pre>" + html.escape(report) + "</pre></details></html>")
    key = Settings().api_key
    for path in output.rglob("*"):
        if path.is_file() and key and key in path.read_text():
            raise ValueError("credential found in artifact export")
    print(json.dumps({"output": str(output), "scores": scores, "routing_skips": skips,
                      "positive_probe_applied": probe_applied, "new_cost_usd": delta}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path("experiments/optimization-001"))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    main(args.root, args.output)
