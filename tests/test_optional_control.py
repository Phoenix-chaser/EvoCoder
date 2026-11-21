import importlib
from collections import Counter
from fractions import Fraction
from pathlib import Path

import pytest

from evocoder.contracts import digest, model_task_view
from evocoder.dataset import load_catalog
from evocoder.optional_benchmark import catalog, freeze_dataset


def driver(name, monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts"))
    return importlib.import_module(name)


def test_fifty_instances_separate_domain_templates_and_grader_inputs(tmp_path):
    root = tmp_path / "bundle"
    manifest = freeze_dataset(root)
    tasks = load_catalog(root / "manifest.json")
    assert len(tasks) == len({t.id for t in tasks}) == len({digest(t.files) for t in tasks}) == 50
    assert Counter(t.split for t in tasks) == {"learn": 10, "dev": 10, "test": 30}
    assert len({t.source_group for t in tasks}) == 10
    assert Counter(r["family"] for r in manifest["tasks"] if r["split"] == "test") == {
        "scalar": 10, "layers": 10, "batch": 10}
    for (_, generated, reference, mutant), loaded in zip(catalog(), tasks, strict=True):
        assert generated == loaded and len({reference, mutant, loaded.files["service.py"]}) == 3
        assert "grade_contract.py" not in model_task_view(loaded.public())["files"]
        assert set(loaded.grader_files).isdisjoint(loaded.files)
        assert "offline contract fixture" not in loaded.issue


def test_distance_uses_distributions_not_trace_length(monkeypatch):
    distance = driver("measure_skill_uptake", monkeypatch).action_distance
    assert distance({"read": 1, "edit": 1}, {"read": 3, "edit": 3}) == 0
    assert distance({"read": 3, "edit": 1}, {"read": 1, "edit": 2, "test": 1}) == Fraction(1, 2)
    with pytest.raises(ValueError, match="at least one"):
        distance({}, {"edit": 1})


def test_first_round_can_only_rule_out_unreachable_release(monkeypatch):
    screen = driver("run_optional_control", monkeypatch).first_round_gate
    one = {"repeat": 0, "split": "dev", "task_id": "a", "source_group": "A", "candidate_pass": True,
           "baseline_pass": False, "infrastructure_error": False, "skill_behavior": "candidate_applied"}
    two = one | {"task_id": "b", "source_group": "B"}
    assert screen([one, two])["still_possible"]
    assert not screen([one, two | {"source_group": "A"}])["still_possible"]
    assert not screen([one, two | {"skill_behavior": "neither_applied"}])["still_possible"]
    assert not screen([one, two, one | {"task_id": "c", "baseline_pass": True, "candidate_pass": False}])["still_possible"]
    with pytest.raises(ValueError):
        screen([one | {"repeat": 1}])


def test_diversity_curve_weights_families_and_preserves_negative_effects(monkeypatch):
    measure = driver("run_optional_control", monkeypatch).curve_point
    win = {"candidate_pass": True, "baseline_pass": False, "family": "scalar"}
    loss = {"candidate_pass": False, "baseline_pass": True, "family": "layers"}
    point = measure([win, loss], "layers", [loss])
    assert point["paired_gain"] == 0 and point["family_gain"] == -1
    with pytest.raises(ValueError, match="equally sized"):
        measure([win, win, loss], "layers", [loss])
