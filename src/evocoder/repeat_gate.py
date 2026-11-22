"""Fixed-denominator release decisions from matched, independently solved trials."""

from collections import Counter, defaultdict
import re

DEV_REPEATS = 5
SUPPORT_REPEATS = 3
REQUIRED_SOURCES = 2


def repeated_gate(rows, expected):
    """expected maps task IDs to their registered split, source group and snapshot hash."""
    groups = defaultdict(list)
    used = set()
    for row in rows:
        task = row["task_id"]
        if task not in expected or tuple(expected[task]) != (
            row["split"],
            row["source_group"],
            row["task_hash"],
        ):
            raise ValueError("unexpected task, source group or snapshot in repeated gate")
        for arm in ("baseline", "candidate"):
            run = row[arm + "_run"]
            if not run or run in used or not re.fullmatch(r"[a-f0-9]{64}", row[arm + "_request"]):
                raise ValueError("missing wire digest or reused paid attempt")
            used.add(run)
            if type(row[arm + "_pass"]) is not bool:
                raise ValueError("invalid repair grade")
        groups[task].append(row)
    if set(groups) != set(expected):
        raise ValueError("incomplete repeated panel")
    details, improved, regressed, errors = {}, set(), [], []
    for task, (split, source, _) in expected.items():
        if split not in {"dev", "learn"}:
            raise ValueError("release gate only accepts development and learning support")
        trials = sorted(groups[task], key=lambda x: x["repeat"])
        m = DEV_REPEATS if split == "dev" else SUPPORT_REPEATS
        if [r["repeat"] for r in trials] != list(range(m)):
            raise ValueError("missing or duplicate registered repeat")
        counts = Counter()
        for row in trials:
            before, after = row["baseline_pass"], row["candidate_pass"]
            outcome = "win" if after and not before else "loss" if before and not after else "tie"
            same = row["baseline_request"] == row["candidate_request"]
            if same and row["baseline_applied_skill"] != row["candidate_applied_skill"]:
                raise ValueError("same wire request cannot contain different skills")
            counts[outcome] += 1
            counts[("same_" if same else "changed_") + outcome] += 1
            if (
                outcome == "win"
                and not same
                and row["skill_behavior"] == "candidate_applied"
                and not row["infrastructure_error"]
                and row.get("evaluation_role") != "known_regression"
            ):
                counts["attributed"] += 1
            if row["infrastructure_error"]:
                errors.append(task)
        k = m // 2 + 1
        changed_gain = counts["changed_win"] - counts["changed_loss"]
        harm = changed_gain < 0 or counts["loss"] >= k
        if harm:
            regressed.append(task)
        confirmed = split == "dev" and counts["attributed"] >= k and not harm and task not in errors
        if confirmed:
            improved.add(source)
        details[task] = {
            "rounds": m,
            "k": k,
            "wins": counts["win"],
            "losses": counts["loss"],
            "ties": counts["tie"],
            "same_wins": counts["same_win"],
            "same_losses": counts["same_loss"],
            "changed_wins": counts["changed_win"],
            "changed_losses": counts["changed_loss"],
            "attributed_wins": counts["attributed"],
            "changed_net": changed_gain,
            "raw_majority_loss": counts["loss"] >= k,
            "confirmed": confirmed,
        }
    return {
        "gate": {
            "policy": "matched_majority_v1",
            "required_sources": REQUIRED_SOURCES,
            "dev_rounds": DEV_REPEATS,
            "support_rounds": SUPPORT_REPEATS,
            "passed": len(improved) >= REQUIRED_SOURCES and not regressed and not errors,
        },
        "repeat_confirmed_improved_groups": sorted(improved),
        "regressions": sorted(regressed),
        "infrastructure_errors": sorted(set(errors)),
        "tasks": details,
    }
