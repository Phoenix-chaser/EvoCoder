import json

import pytest
from pydantic import ValidationError

from evocoder.contracts import Action, Settings, Skill, digest, safe_path
from evocoder.dataset import create_fixtures, fixture_task, load_catalog
from evocoder.model import Model


@pytest.mark.parametrize("path", ["../a.py", "/tmp/a", "a/../../b", ".env", "src/.key", "a\\b", ""])
def test_paths_cannot_escape_or_export_hidden_data(path):
    with pytest.raises(ValueError):
        safe_path(path)


def test_task_rejects_grader_exposure_and_unknown_fields():
    task = fixture_task("a", "learn", False).model_dump()
    task["grader_files"] = {"public_tests.py": "bad"}
    with pytest.raises(ValidationError, match="absent"):
        type(fixture_task("a", "learn", False)).model_validate(task)
    with pytest.raises(ValidationError):
        Action(tool="finish", reason="Done", success=True)


@pytest.mark.parametrize("updates", [
    {}, {"base_url": "https://user:key@example.com"}, {"budget_usd": 0},
    {"input_price_per_million": -1}, {"output_price_per_million": float("nan")},
])
def test_live_never_silently_falls_back(updates):
    options = {"mode": "live", "base_url": "https://model.example/v1", "api_key": "unit-key",
               "model": "unit-model", "budget_usd": 1,
               "input_price_per_million": 1, "output_price_per_million": 1}
    if not updates:
        options.pop("api_key")
    options.update(updates)
    with pytest.raises(ValueError):
        Settings(_env_file=None, **options).validate_live()


def test_manifest_freeze_and_split_group_isolation(tmp_path):
    create_fixtures(tmp_path)
    tasks = load_catalog(tmp_path / "manifest.json")
    assert len(tasks) == 12
    task_path = tmp_path / "dev-01.json"
    task = json.loads(task_path.read_text())
    task["issue"] += " changed"
    task_path.write_text(json.dumps(task))
    with pytest.raises(ValueError, match="hash"):
        load_catalog(tmp_path / "manifest.json")


def test_hash_covers_procedural_content_and_mock_is_explicit():
    data = {"parent": None, "evidence": {"episodes": [{"run_id": "r", "public_failed": False}]}}
    skill = Model.mock_skill(data)
    revised = Skill.model_validate({**skill.model_dump(), "exclusions": ["Different boundary"]})
    assert skill.hash != revised.hash
    assert digest({"a": 1, "b": 2}) == digest({"b": 2, "a": 1})
    with pytest.raises(ValueError, match="mock only"):
        Model.mock_action({"task": {"issue": "real task"}})
