"""Read-only GLM pilot/comparison export, with credits kept distinct from USD."""

import argparse
import html
import json
import shutil
import sqlite3
from contextlib import closing
from difflib import SequenceMatcher
from pathlib import Path
from urllib.parse import quote

from dotenv import dotenv_values

from evocoder.contracts import digest
from evocoder.statistics import summarize


def open_report_db(path: Path, *, sealed: bool = False):
    wal = path.with_name(path.name + "-wal")
    query = "?mode=ro"
    # A finished, checkpointed archive can be exported from read-only media.
    if sealed and not (wal.exists() and wal.stat().st_size):
        query += "&immutable=1"
    return closing(sqlite3.connect(path.resolve().as_uri() + query, uri=True))


def diagnostic_text(protocol: dict, result: dict) -> str:
    learning = result.get("follow_up", [])
    used = sum(trial["applied_skill"] == protocol["parent"] for trial in learning)
    eligible = sum(trial["eligible_for_revision"] for trial in learning)
    learning_stage = "复用已核验学习轨迹" if result.get("reused_learning") else "学习执行"
    text = (
        f"{learning_stage} {len(learning)}/{len(protocol['follow_up_tasks'])}，父技能实际应用 {used} 次，"
        f"符合修订条件的轨迹 {eligible} 条。发起提案 {result['proposal_attempts']} 次。"
        f"本轮新增已结算估算 {result['new_cost_credits']:.5f} 积分。\n\n"
    )
    if result["candidate"]:
        text += f"父版本 {protocol['parent']}\n子版本 {result['candidate']}\n\n"
    if rejected := protocol.get("rejected_predecessor"):
        text += f"上轮退化的子版本 {rejected}\n\n"
    comparison = result.get("diagnostic")
    paired = comparison["rows"] if comparison else result.get("paired_rows", [])
    if paired:
        for label, ids in (
            ("新增开发" if "regression_tasks" in protocol else "开发", set(protocol["dev_tasks"])),
            ("已知回归", set(protocol.get("regression_tasks", []))),
            ("支持回放", {row["task_id"] for row in paired if row["split"] == "learn"}),
        ):
            pairs = [row for row in paired if row["task_id"] in ids]
            if not pairs:
                continue
            parent_passes = sum(row["baseline_pass"] for row in pairs)
            child_passes = sum(row["candidate_pass"] for row in pairs)
            text += f"{label} {len(pairs)} 对，父版通过 {parent_passes} 次，子版通过 {child_passes} 次。\n"
    if comparison:
        text += (
            f"开发门禁{'通过' if comparison['gate']['passed'] else '未通过'}。"
            f"重复确认改善组 {len(comparison['repeat_confirmed_improved_groups'])}/"
            f"{comparison['gate']['required_improved_groups']}，"
            f"观察到退化的任务 {len(comparison['regressions'])} 道。\n\n"
        )
    holdout = result.get("holdout", {})
    if holdout.get("status") == "complete":
        pairs = len(holdout["rows"])
        text += (
            f"留出共 {pairs} 对。父版通过 {holdout['parent_passes']}/{pairs}，"
            f"子版通过 {holdout['child_passes']}/{pairs}，"
            f"子版实际应用 {holdout['candidate_applications']} 次，"
            f"改善 {holdout['wins']} 对，退化 {holdout['regressions']} 对。\n\n"
        )
    elif protocol["test_tasks"]:
        status = holdout.get("status")
        if status == "blocked_by_dev_gate":
            text += "开发门禁挡住留出阶段，留出调用为零。\n\n"
        elif status == "blocked_no_candidate":
            text += "本轮止于候选生成前后，留出调用为零。\n\n"
        else:
            text += f"留出共记录 {len(holdout.get('rows', []))} 个完整配对，状态为 {status}。\n\n"
    return text


def grounded_report(db, root, output, protocol, result, proposals):
    from evocoder.grounding import validate_summary

    def artifact(key):
        row = db.execute("SELECT body FROM artifacts WHERE id=?", (key,)).fetchone()
        if row is None:
            raise ValueError(f"missing grounding artifact {key}")
        body = json.loads(row[0])
        if digest(body) != key:
            raise ValueError(f"grounding artifact digest mismatch {key}")
        return body

    check = json.loads((root / "grounding-check.json").read_text())
    if digest(check) != protocol["grounding_check_hash"]:
        raise ValueError("grounding control receipt changed")
    current = result or {}
    if not current and (root / "progress.json").is_file():
        current = json.loads((root / "progress.json").read_text())
    candidate = current.get("candidate")
    starts = [
        (p, event["payload"]) for p in proposals for event in p["events"]
        if event["kind"] == "PROPOSAL_START" and event["payload"].get("proposal_policy") == "grounded_patch_tests"
    ]
    if len(starts) > 1 or (not starts and candidate):
        raise ValueError("candidate must bind to one grounded proposal")
    status = "尚无已保存的候选"
    detail = current.get("proposal_rejection") or current.get("error")
    info = {"control_hash": digest(check), "parent": protocol["parent"], "candidate": candidate,
            "detail": detail, "changes": [], "verified": False}
    if starts:
        proposal, start = starts[0]
        if start["parent_id"] != protocol["parent"]:
            raise ValueError("grounded proposal has a different parent")
        catalog = artifact(start["grounding_catalog"])
        evidence = artifact(start["evidence_hash"])
        if set(catalog["receipts"]) != {e["run_id"] for e in evidence["episodes"]}:
            raise ValueError("grounding catalog cites different learning runs")
        info.update(proposal_id=proposal["proposal_id"], catalog=catalog, catalog_hash=start["grounding_catalog"])
        rejected = [e["payload"] for e in proposal["events"] if e["kind"] == "SUMMARY_REJECTED"]
        if rejected:
            if candidate or len(rejected) != 1 or rejected[0]["catalog"] != start["grounding_catalog"]:
                raise ValueError("rejection conflicts with accepted candidate or grounding catalog")
            info["rejected_summary"] = artifact(rejected[0]["proposal_output"])["change_summary"]
            status = "修订摘要被拒绝"
        elif detail:
            status = "提案或运行已停止"
        if candidate:
            verified = [e["payload"] for e in proposal["events"] if e["kind"] == "SUMMARY_VERIFIED"]
            if len(verified) != 1 or verified[0]["candidate"] != candidate:
                raise ValueError("missing verified summary for admitted candidate")
            receipt = artifact(verified[0]["artifact"])
            parent = artifact(protocol["parent"])
            child = artifact(candidate)
            refs = child["evidence_refs"]
            original = artifact(receipt["proposal_output"])
            recorded_outputs = [e["payload"]["artifact"] for e in proposal["events"] if e["kind"] == "PROPOSAL_OUTPUT"]
            if (receipt["schema"] != 1 or receipt["candidate"] != candidate or receipt["evidence_refs"] != refs
                    or receipt["parent"] != child["parent"] or child["parent"] != protocol["parent"]
                    or receipt["frozen_evidence"] != start["evidence_hash"] or receipt["catalog"] != start["grounding_catalog"]
                    or receipt["summary_hash"] != digest(child["change_summary"])
                    or recorded_outputs != [receipt["proposal_output"]]
                    or {**original, "change_summary": child["change_summary"]} != child):
                raise ValueError("verified receipt differs from proposed or persisted skill")
            validate_summary(child["change_summary"], refs, catalog)
            fields = ("applicability", "steps", "verification", "exclusions", "counterexamples")
            changed = {k: {"before": digest(parent[k]), "after": digest(child[k])} for k in fields if parent[k] != child[k]}
            if not changed or changed != receipt["changed_fields"]:
                raise ValueError("procedural edits differ from verified receipt")
            for field in changed:
                before, after = parent[field], child[field]
                for op, i, j, m, n in SequenceMatcher(None, before, after, autojunk=False).get_opcodes():
                    if op != "equal":
                        info["changes"].append({"field": field, "operation": op, "parent_start": i + 1,
                                                "child_start": m + 1, "before": before[i:j], "after": after[m:n]})
            info.update(receipt=receipt, receipt_hash=verified[0]["artifact"], verified=True,
                        verified_summary=child["change_summary"])
            status = "候选摘要已校验"
    info["status"] = status
    write_grounded_report(output, info)
    pairs = (current.get("diagnostic") or {}).get("rows", current.get("paired_rows", []))
    return {"status": status, "verified": info["verified"], "candidate": candidate, "paired_count": len(pairs),
            "receipt_hash": info.get("receipt_hash"), "control_hash": info["control_hash"]}


def write_grounded_report(output, info):
    (output / "grounding.json").write_text(json.dumps(info, ensure_ascii=False, indent=2) + "\n")
    esc = html.escape
    parts = ["<!doctype html><html lang='zh-CN'><meta charset='utf-8'><title>技能修订证据</title>",
             "<style>body{font:16px/1.6 system-ui;max-width:960px;margin:2rem auto;padding:0 1rem}pre{white-space:pre-wrap}li,p{overflow-wrap:anywhere}</style>",
             "<h1>技能修订证据</h1><p><a href='report.html'>实验结果</a> · "
             "<a href='grounding-check.json'>Click #3509 对照回执</a> · <a href='grounding.json'>结构化记录</a></p>",
             f"<h2>{esc(info['status'])}</h2>"]
    if info["detail"]:
        parts.append(f"<p>{esc(info['detail'])}</p>")
    if "catalog" in info:
        parts.append("<h2>学习记录中的事实</h2>")
        if info["verified"]:
            parts.append(f"<pre>{esc(info['verified_summary'])}</pre>")
        parts.append("<ul>")
        references = info["receipt"]["evidence_refs"] if info["verified"] else info["catalog"]["receipts"]
        for key in references:
            row = info["catalog"]["receipts"][key]
            parts.append(f"<li><a href='traces/seed/{quote(key, safe='')}.json'>{esc(row['task_id'])} 执行轨迹</a>"
                         f" · 改动文件 {len(row['files'])} 个 · 公开检查 {esc(row['public_check']['status'])}"
                         f" · 最终评分 {esc(row['final_grade']['status'])}</li>")
        parts.append("</ul>")
    if info["verified"]:
        parts.append("<h2>候选提出的操作变更</h2><p>下列顺序及文字取自两份技能。学习记录是本次提案的依据，"
                     "跨任务修复效果由父子配对和留出评测判定。</p>")
        labels = {"applicability": "适用条件", "steps": "操作步骤", "verification": "验证方法",
                  "exclusions": "排除条件", "counterexamples": "反例"}
        for change in info["changes"]:
            parts.append(f"<h3>{labels[change['field']]}</h3>")
            for items, begin, label in (("before", "parent_start", "父版"), ("after", "child_start", "子版提案")):
                if change[items]:
                    parts.append(f"<p>{label}</p><ol start='{change[begin]}'>")
                    parts.extend(f"<li>{esc(item)}</li>" for item in change[items])
                    parts.append("</ol>")
    elif "rejected_summary" in info:
        parts.append("<h3>已拒绝的模型原文</h3><pre>" + esc(info["rejected_summary"]) + "</pre>")
    parts.append("</html>")
    (output / "grounding.html").write_text("\n".join(parts))


def export(root: Path, output: Path):
    protocol = json.loads((root / "protocol.json").read_text())
    if protocol["settings"]["billing_mode"] != "subscription_credits":
        raise ValueError("credit report only accepts a subscription-credit protocol")
    result = json.loads((root / "result.json").read_text()) if (root / "result.json").exists() else None
    output.mkdir(parents=True, exist_ok=True)
    shutil.copytree(root / "source", output / "source", dirs_exist_ok=True)
    shutil.copy2(root / "protocol.json", output / "protocol.json")
    for name in [
        "result.json",
        "probe.json",
        "trace.json",
        "skill.json",
        "frozen-test-state.json",
        "experience-pool.json",
        "seed-reports.json",
        "runner.py",
        "stop-audit.json",
        "preparation-audit.json",
        "grounding-check.json",
    ]:
        if (root / name).is_file():
            shutil.copy2(root / name, output / name)
    for name in ["learning-C", "learning-D", "cells", "diagnosis"]:
        if (root / name).is_dir():
            shutil.copytree(root / name, output / name, dirs_exist_ok=True)
    summary = {
        "protocol": protocol,
        "result": result,
        "billing_unit": "credits",
        "actual_subscription_cost_usd": None,
        "rows": [],
        "lineage": {},
        "proposal_records": {},
        "supports_revision_benefit": False,
    }
    expected_source = protocol["source_hash"]
    if (root / "stop-audit.json").exists():
        summary["stop_audit"] = json.loads((root / "stop-audit.json").read_text())
    if (root / "preparation-audit.json").exists():
        summary["preparation_audit"] = json.loads((root / "preparation-audit.json").read_text())
    assert digest({p.name: p.read_text() for p in (root / "source").glob("*.py")}) == expected_source
    folders = ["state"] if (root / "state").exists() else ["common", "B", "seed", "C", "D"]
    for folder in folders:
        path = root / folder / "evocoder.sqlite3"
        if not path.exists():
            continue
        with open_report_db(path, sealed=result is not None) as db:
            db.row_factory = sqlite3.Row
            assert db.execute("SELECT unit FROM accounting_unit").fetchone()[0] == "credits"
            lineage = []
            for r in db.execute("SELECT * FROM skills ORDER BY rowid"):
                item = dict(r)
                for key, identity in [("skill", item["id"]), ("evidence", item["evidence_hash"])]:
                    body = json.loads(
                        db.execute("SELECT body FROM artifacts WHERE id=?", (identity,)).fetchone()[0]
                    )
                    assert digest(body) == identity
                    item[key] = body
                lineage.append(item)
            summary["lineage"][folder] = lineage
            proposals = []
            for r in db.execute("SELECT DISTINCT run_id FROM events WHERE kind='PROPOSAL_START'"):
                records = []
                for e in db.execute("SELECT seq,kind,payload FROM events WHERE run_id=? ORDER BY seq", r):
                    event = dict(e)
                    event["payload"] = json.loads(event["payload"])
                    records.append(event)
                proposals.append({"proposal_id": r[0], "events": records})
            outputs = []
            for r in db.execute(
                "SELECT id,kind,body FROM artifacts WHERE kind IN "
                "('proposal_output','model_proposal_content','model_library_selection','rejected_model_arguments') ORDER BY created"
            ):
                item = dict(r)
                item["body"] = json.loads(item["body"])
                assert digest(item["body"]) == item["id"]
                outputs.append(item)
            summary["proposal_records"][folder] = {"proposals": proposals, "outputs": outputs}
            if folder == "seed" and protocol.get("proposal_policy") == "grounded_patch_tests":
                summary["grounding"] = grounded_report(db, root, output, protocol, result, proposals)
            target = output / "traces" / folder
            target.mkdir(parents=True, exist_ok=True)
            for r in db.execute("SELECT * FROM runs ORDER BY created"):
                run = dict(r)
                run["config"] = json.loads(run["config"])
                run["result"] = json.loads(run["result"]) if run["result"] else None
                events = [
                    dict(e)
                    for e in db.execute(
                        "SELECT seq,kind,payload FROM events WHERE run_id=? ORDER BY seq", (run["id"],)
                    )
                ]
                for e in events:
                    e["payload"] = json.loads(e["payload"])
                (target / (run["id"] + ".json")).write_text(
                    json.dumps({"run": run, "events": events}, ensure_ascii=False)
                )
    rows = [json.loads(f.read_text()) for f in (root / "cells").glob("*.json")]
    rows = [r for r in rows if r["state"] != "started"]
    summary["rows"] = rows
    if "arms" in protocol:
        manifest = json.loads(Path(protocol["manifest"]).read_text())
        ids = [t["id"] for t in manifest["tasks"] if t["split"] == "test"]
        stats = summarize(rows, ids, protocol["repeats"], cost_unit="credits")
        summary["statistics"] = stats
        revisions = {r["id"] for r in summary["lineage"].get("D", []) if r["parent"]}
        applied = [r for r in rows if r["arm"] == "D" and r["applied_skill_id"] in revisions]
        summary["actual_revised_skill_applications"] = len(applied)
        summary["test_preflight"] = result.get("test_preflight") if result else None
        summary["supports_revision_benefit"] = bool(stats["supports_positive_primary_effect"] and applied)
        for row in rows:
            folder = "common" if row["arm"] == "A" else row["arm"]
            with open_report_db(root / folder / "evocoder.sqlite3", sealed=result is not None) as db:
                actual = db.execute(
                    "SELECT task_id,split,purpose,result FROM runs WHERE id=?", (row["run_id"],)
                ).fetchone()
                assert actual and actual[:3] == (row["task_id"], "test", "evaluate")
                actual_result = json.loads(actual[3])
                assert actual_result["passed"] == row["passed"]
                if "cost" in actual_result:
                    assert actual_result["cost"]["cost_credits"] == row["cost_credits"]
    if result:
        summary["budget"] = result["budget_after"]
    else:
        ledger = Path(protocol.get("ledger_root", protocol["settings"]["state_dir"])) / "evocoder.sqlite3"
        with open_report_db(ledger) as db:
            cap, spent, reserved = db.execute("SELECT cap,spent,reserved FROM budget").fetchone()
            summary["budget"] = {
                "cap_credits": None if cap == (1 << 63) - 1 else cap / 1e9,
                "spent_credits": spent / 1e9,
                "reserved_credits": reserved / 1e9,
            }
    skipped = bool(result and result.get("phase") == "no_revised_library")
    status = "已完成" if result and result["completed"] else "已停止／未完成" if result else "运行中"
    if skipped:
        status = "未进入终评"
    if protocol.get("claim") == "parent_child_diagnostic":
        note = "这是同一技能家族的小样本父子诊断，可检查实际应用、触发与配对结果。"
    elif "arms" in protocol:
        note = "仅完整矩阵且实际应用修订技能后，才可依据预定统计标准判断本轮收益。"
    else:
        note = "这是接口、执行、评分与提案验证，不能证明自进化收益。"
    text = (
        f"# EvoCoder GLM-5.3\n\n状态：{status}。{note}\n\n"
        "本报告的用量单位是套餐积分，按最高时段系数保守估算，不是美元或实际订阅账单。"
        "无本地积分总量上限不等于平台无限配额。\n\n"
        f"用量：{json.dumps(summary['budget'], ensure_ascii=False)}\n\n"
        "DeepSeek 的历史费用、未结算请求和部分成绩独立保留，不与 GLM 合并。\n\n"
    )
    if protocol.get("cross_model_replication"):
        text += "本轮为跨模型复现，部分测试题已由控制器在 DeepSeek 上评测；不是控制器盲测。旧测试轨迹不进入 GLM 学习证据。\n\n"

    skill_arm_rows = [r for r in rows if r["arm"] in {"C", "D"}]
    if skipped:
        text += "终评前检查未找到同一套技能的已发布版本对照。终评调用数为 0。\n\n"
    elif skill_arm_rows and all(r["library_id"] is None for r in skill_arm_rows):
        text += "已记录的 C、D 终评均未使用技能库。两组差值无法归因于技能修订。\n\n"
    if summary.get("stop_audit", {}).get("reason_code") == "routing_contract_invalid_null_identifiers":
        text += "本轮因路由合同问题停止：部分跳过决定生成了非空技能 ID（可见原始输出含字符串 null），被安全拒绝。保留 32 例共同学习、3 个根技能和全部回放；未开始 C/D 学习或终评，不能解释为自进化或收益证据。所有已派发调用均结算，预留为零。\n\n"
    if summary.get("stop_audit", {}).get("reason_code") == "solver_command_deadline_classified_as_infrastructure":
        text += "本轮在第三个根技能门禁中停止：Agent 把指定单文件测试扩大为整个目录，命令超过 20 秒预算。离线复现表明扩大后的命令在原始代码和补丁上都超时，而原指定命令在两者上均通过 26 项公开测试。旧 ERROR 保留，没有补跑覆盖；未开始 C/D 学习或终评。全部已派发调用已结算，预留为零。诊断细节见 stop-audit.json 与 diagnosis/。\n\n"
    if summary.get("stop_audit", {}).get("reason_code") == "provider_http_500":
        text += "智谱 HTTP 500 中止本轮：C 组保留 12 条记录（8 通过、2 失败、1 预算耗尽、1 接口错误），D 学习和 384 次终评尚未开始。没有新修订或发布，不能证明自进化收益。该次请求未收到可结算 usage，保留 42.74130 积分预留，不自动重发或释放。\n\n"
    if result and protocol.get("claim") == "parent_child_diagnostic":
        text += diagnostic_text(protocol, result)
    if result and result.get("error"):
        text += "停止原因：" + result["error"] + "\n\n"
    grounding_link = ""
    if "grounding" in summary:
        ground = summary["grounding"]
        text += f"{ground['status']}，已记录 {ground['paired_count']} 个完整配对。[修订摘要、操作差异与学习证据](grounding.html)\n\n"
        grounding_link = ' · <a href="grounding.html">修订摘要、操作差异与学习证据</a>'
    (output / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
    (output / "report.md").write_text(text)
    diagnosis_link = (
        ' · <a href="diagnosis/paired-traces.json">父子配对与补丁复核</a>'
        if (root / "diagnosis" / "paired-traces.json").is_file() else ""
    )
    (output / "report.html").write_text(
        '<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>EvoCoder GLM</title>'
        "<style>body{font:16px/1.7 system-ui;max-width:1050px;margin:32px auto;padding:0 24px}pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#f1f5f7;padding:20px}</style>"
        '<h1>EvoCoder GLM-5.3</h1><p><a href="summary.json">完整结果与演化证据</a> · <a href="protocol.json">冻结协议</a>'
        + diagnosis_link + grounding_link + "</p><pre>"
        + html.escape(text + json.dumps(summary.get("statistics", result), ensure_ascii=False, indent=2))
        + "</pre></html>"
    )
    secrets = {
        dotenv_values(p).get("EVOCODER_API_KEY") for p in [Path(".env"), Path(".env.glm")] if p.exists()
    }
    for path in output.rglob("*"):
        if path.is_file():
            data = path.read_bytes()
            if any(key and key.encode() in data for key in secrets):
                raise ValueError("configured credential found in export")
    counts = {"cells": len(rows)}
    if "grounding" in summary:
        counts = {"diagnostic_pairs": summary["grounding"]["paired_count"]}
    elif result and protocol.get("claim") == "parent_child_diagnostic":
        pairs = (result.get("diagnostic") or {}).get("rows", result.get("paired_rows", []))
        counts = {"diagnostic_pairs": len(pairs)}
    print({"status": status, **counts, "unit": "credits"})


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    export(args.root, args.output)
