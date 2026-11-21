"""Verify the contents of the historical Click sample with Python's standard library."""

import argparse
import hashlib
import json
from pathlib import Path


def digest(value):
    content = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(content).hexdigest()


def verify(sample):
    parent, candidate = sample["parent"], sample["candidate"]
    failure = sample["failure_excerpt"]
    receipt = sample["receipt"]
    report = receipt["report"]
    artifacts = [(parent["id"], parent["skill"]), (candidate["id"], candidate["skill"]), (receipt["id"], report)]
    if sample["schema"] != 1 or any(digest(body) != key for key, body in artifacts):
        raise ValueError("技能或收据哈希与历史记录不一致")
    if (
        candidate["skill"]["parent"] != parent["id"]
        or failure["applied_skill_id"] != parent["id"]
        or failure["route"] != "apply"
        or failure["run_id"] not in candidate["skill"]["evidence_refs"]
        or report["candidate"] != candidate["id"]
        or candidate["id"] not in report["members"]
    ):
        raise ValueError("失败、父子技能与收据未形成同一条证据链")

    writes = checks_passed = 0
    for row in failure["observations"]:
        if row["tool"] in ("write", "edit") and row["exit_code"] == 0:
            writes += 1
        if writes and row["public_check"] and row["exit_code"] == 0:
            checks_passed += 1
    feedback = failure["feedback"]
    if (
        failure["state"] != "EXHAUSTED"
        or not feedback["step_budget_exhausted"]
        or (writes, checks_passed) != (feedback["write_count"], feedback["public_success_count_after_edit"])
    ):
        raise ValueError("失败摘录与已记录的执行反馈不一致")

    rows = report["rows"]
    wins = sorted({
        row["source_group"] for row in rows
        if row["split"] == "dev" and row["candidate_pass"] and not row["baseline_pass"]
    })
    regressions = [row["task_id"] for row in rows if row["baseline_pass"] and not row["candidate_pass"]]
    errors = [row["task_id"] for row in rows if row["infrastructure_error"]]
    gate = report["gate"]
    passed = len(wins) >= gate["required_improved_groups"] and not regressions and not errors
    if (wins, regressions, errors, passed) != (
        report["improved_groups"], report["regressions"], report["infrastructure_errors"], gate["passed"],
    ):
        raise ValueError("根据逐例记录重算的历史发布判定和原收据不一致")
    return {
        "case": failure["issue_excerpt"],
        "model": sample["origin"]["provider_model"],
        "parent": parent["id"],
        "candidate": candidate["id"],
        "latest_failure": f'{failure["model_steps"]} 步耗尽，{writes} 次编辑，编辑后公开检查通过 {checks_passed} 次',
        "steps_before": parent["skill"]["steps"][0],
        "steps_after": candidate["skill"]["steps"][0],
        "historical_gate_baseline": report["baseline_library"] or "空发布库",
        "dev_improved_groups": len(wins),
        "regressions": regressions,
        "historical_gate_passed": passed,
        "released": False if not passed else "需核对发布日志",
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "sample",
        nargs="?",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "examples/click-revision.json",
    )
    args = parser.parse_args()
    print(json.dumps(verify(json.loads(args.sample.read_text())), ensure_ascii=False, indent=2))
