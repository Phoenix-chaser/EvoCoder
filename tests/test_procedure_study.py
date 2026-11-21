import sys
from pathlib import Path

import pytest

from evocoder.comparison import dump
from evocoder.contracts import digest
from evocoder.dataset import fixture_task

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from run_click_procedure import check_learning, gate_still_possible  # noqa: E402


def paired(task, repeat, before, after, changed=True):
    return {"split": "dev", "task_id": task, "source_group": task, "repeat": repeat,
            "baseline_pass": before, "candidate_pass": after, "infrastructure_error": False,
            "skill_behavior": "candidate_applied" if changed else "neither_applied"}


def test_repeated_skill_related_win_allows_remaining_gate_but_not_holdout():
    rows = [paired("first", i, False, True) for i in range(2)] + [paired("second", i, True, True) for i in range(2)]
    possible, summary = gate_still_possible(rows, 1)
    assert possible and not summary["gate"]["passed"]
    assert not gate_still_possible(rows[:3] + [paired("second", 1, True, False)], 1)[0]
    assert not gate_still_possible([paired("first", 0, False, True), paired("first", 1, False, True, False)], 1)[0]


def test_independent_learn_requires_original_failure_and_distinct_historical_group(tmp_path):
    tasks = [fixture_task(f"learn-{i}", "learn", bool(i)).model_copy(update={"source_group": f"group-{i}"})
             for i in range(2)]
    path = tmp_path / "fresh" / "manifest.json"
    manifest = {"tasks": [{"id": t.id, "hash": digest(t.model_dump()), "source_group": t.source_group} for t in tasks]}
    dump(path, manifest)
    checks = {k: {"exit_code": int(k.startswith("buggy")), "junit": {"errors": 0}, "image_id": "image"}
              for k in ("buggy_public", "buggy_regression", "reference_public", "reference_regression")}
    proof = {"passed": True, "model_calls": 0, "manifest_hash": digest(manifest), "image_id": "image",
             "rows": [{"id": t.id, "passed": True, "image_id": "image", "checks": checks, "task_hash": digest(t.model_dump())}
                      for t in tasks]}
    dump(path.parent / "verification.json", proof)
    check_learning(tasks, path, "image", tmp_path)
    history = tmp_path / "previous" / "manifest.json"
    dump(history, {"tasks": [{"id": "different", "source_group": tasks[1].source_group}]})
    with pytest.raises(ValueError, match="overlaps"):
        check_learning(tasks, path, "image", tmp_path)
    dump(history, {"tasks": []})
    checks["buggy_public"]["exit_code"] = 0
    dump(path.parent / "verification.json", proof)
    with pytest.raises(ValueError, match="verification outcome"):
        check_learning(tasks, path, "image", tmp_path)
