import re
from copy import deepcopy

import pytest

from evocoder.route_analysis import analyze_routes, write_route_report


TASKS = {"count": "count-help", "hint": "usage-hint"}
PROTOCOL = {"tasks": TASKS, "parent": "p", "child": "c", "repeats": 2,
            "settings": {"model": "unit", "temperature": 1, "max_steps": 16}}


def matrix():
    rows = []
    for task, group in TASKS.items():
        for repeat in range(2):
            for policy in ("normal", "fixed_apply", "fixed_skip"):
                row = {"task_id": task, "source_group": group, "split": "dev", "repeat": repeat,
                       "task_hash": "hash-" + task, "policy": policy, "infrastructure_error": False}
                for arm, version in (("baseline", "p"), ("candidate", "c")):
                    applied = policy != "fixed_skip"
                    row.update({arm + "_run": f"{arm}-{task}-{repeat}-{policy}",
                                arm + "_pass": arm == "candidate",
                                arm + "_applied_skill": version if applied else None,
                                arm + "_route": "apply" if applied else "skip",
                                arm + "_route_source": "fixed_intervention" if policy != "normal" else None,
                                arm + "_model_steps": 4, arm + "_credits": 1.02})
                rows.append(row)
    return rows


def analyze(rows):
    return analyze_routes(rows, tasks=TASKS, parent="p", child="c")


def test_intervened_wins_cannot_pass_the_normal_gate():
    rows = matrix()
    assert analyze(rows)["gate"]["passed"]
    rows[0]["baseline_pass"] = True
    analysis = analyze(rows)
    assert analysis["gate"]["passed"]  # One remaining dev group can still complete the original gate.
    assert analysis["gate"]["normal"]["repeat_confirmed_improved_groups"] == ["usage-hint"]
    assert analysis["by_policy"]["fixed_apply"]["wins"] == 4
    assert analysis["by_policy"]["fixed_skip"]["candidate"]["credits"] == 4.08
    rows[6]["baseline_pass"] = True
    assert analyze(rows)["gate"]["blockers"] == ["insufficient_repeated_wins"]


def test_normal_wins_need_actual_skill_and_skip_control_is_noise():
    rows = matrix()
    for row in rows:
        if row["policy"] == "fixed_skip":
            row.update(baseline_pass=True, candidate_pass=False)
    assert analyze(rows)["gate"]["passed"]
    rows[0].update(candidate_route="skip", candidate_applied_skill=None,
                   baseline_route="skip", baseline_applied_skill=None)
    analysis = analyze(rows)
    assert "count-help" not in analysis["gate"]["normal"]["repeat_confirmed_improved_groups"]
    assert analysis["attribution"]["count"]["candidate"]["normal_injection_changed"]
    assert analysis["gate"]["normal"]["wins_without_skill_change"]


def test_fixed_apply_regression_is_attribution_while_missing_or_infrastructure_blocks():
    rows = matrix()
    rows[1].update(baseline_pass=True, candidate_pass=False)
    analysis = analyze(rows)
    assert analysis["gate"]["passed"]
    assert analysis["gate"]["fixed_apply_regressions"] == [{"task_id": "count", "repeat": 0}]
    assert "incomplete_matrix" in analyze(rows[:-1])["gate"]["blockers"]
    rows[-1]["infrastructure_error"] = True
    assert "infrastructure_error" in analyze(rows)["gate"]["blockers"]
    rows[0].update(baseline_pass=True, candidate_pass=False)
    assert "normal_regression" in analyze(rows)["gate"]["blockers"]


@pytest.mark.parametrize("defect", ["duplicate", "reused_run", "false_intervention", "wrong_skill",
                                    "bad_snapshot", "unknown_task", "normal_intervention", "unequal_memory"])
def test_pair_label_and_provenance_checked(defect):
    rows = matrix()
    if defect == "duplicate":
        rows.append(deepcopy(rows[-1]))
    elif defect == "reused_run":
        rows[-1]["baseline_run"] = rows[0]["baseline_run"]
    elif defect == "false_intervention":
        rows[1].update(candidate_route="skip", candidate_applied_skill=None)
    elif defect == "wrong_skill":
        rows[0]["baseline_applied_skill"] = "c"
    elif defect == "bad_snapshot":
        rows[1]["task_hash"] = "changed"
    elif defect == "unknown_task":
        rows[-1]["task_id"] = "holdout"
    elif defect == "normal_intervention":
        rows[0]["candidate_route_source"] = "fixed_intervention"
    else:
        rows[2]["candidate_solver_memory_hash"] = "mismatch"
    with pytest.raises(ValueError):
        analyze(rows)


def test_per_arm_paired_apply_vs_skip_and_report_links(tmp_path):
    rows = matrix()
    rows[2]["candidate_pass"] = False
    analysis = analyze(rows)
    assert analysis["attribution"]["count"]["candidate"]["only_apply_pass"] == 1
    result = {"completed": True, "rows": rows, "analysis": analysis, "new_cost_credits": 24.48,
              "holdout": {"status": "blocked", "rows": []}, "full_gate": {"status": "blocked", "rows": []}}
    write_route_report(tmp_path, PROTOCOL, result)
    page = (tmp_path / "report.html").read_text()
    links = re.findall('href="(traces/[^"]+)"', page)
    assert len(links) == len(set(links)) == 24
    assert "固定应用" in page and "24.48000" in page and "blocked" in page
    rows[0]["baseline_pass"] = True
    with pytest.raises(ValueError, match="disagrees"):
        write_route_report(tmp_path, PROTOCOL, result)
