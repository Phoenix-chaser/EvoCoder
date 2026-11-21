"""Release decisions from paired runs and observable changes in skill use."""

from collections import defaultdict


def skill_behavior(row: dict, candidate: str, replaced: set[str]) -> str:
    before = row["baseline_applied_skill"]
    after = row["candidate_applied_skill"]
    if after == candidate and before != candidate:
        return "candidate_applied"
    if before in replaced and after is None and row.get("candidate_route") == "skip":
        return "replaced_version_skipped"
    if before is None and after is None:
        return "neither_applied"
    return "other_or_unchanged"


def gate_summary(rows: list[dict], dev_repeats: int) -> dict:
    wins_by_task = defaultdict(list)
    for row in rows:
        if (
            row["split"] == "dev" and row.get("evaluation_role") != "known_regression"
            and row["candidate_pass"] and not row["baseline_pass"]
        ):
            wins_by_task[row["task_id"]].append(row)

    confirmed_groups = {
        wins[0]["source_group"]
        for wins in wins_by_task.values()
        if {row["repeat"] for row in wins} == set(range(dev_repeats))
        and all(
            not row["infrastructure_error"]
            and row["skill_behavior"] in {"candidate_applied", "replaced_version_skipped"}
            for row in wins
        )
    }
    regressions = list(dict.fromkeys(
        row["task_id"] for row in rows if row["baseline_pass"] and not row["candidate_pass"]
    ))
    errors = list(dict.fromkeys(row["task_id"] for row in rows if row["infrastructure_error"]))
    return {
        "gate": {
            "policy": "repeated_wins_with_skill_behavior",
            "required_improved_groups": 2,
            "required_dev_repeats": dev_repeats,
            "zero_observed_regressions": True,
            "passed": len(confirmed_groups) >= 2 and not regressions and not errors,
        },
        "improved_groups": sorted({row["source_group"] for wins in wins_by_task.values() for row in wins}),
        "repeat_confirmed_improved_groups": sorted(confirmed_groups),
        "wins_without_skill_change": [
            {"task_id": row["task_id"], "repeat": row["repeat"], "skill_behavior": row["skill_behavior"]}
            for wins in wins_by_task.values() for row in wins
            if row["skill_behavior"] in {"neither_applied", "other_or_unchanged"}
        ],
        "regressions": regressions,
        "infrastructure_errors": errors,
    }
