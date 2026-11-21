import importlib.util
from pathlib import Path

import pytest


spec = importlib.util.spec_from_file_location("click_checkpoint_replay", Path(__file__).parents[1] / "scripts/replay_click_checkpoint.py")
replay = importlib.util.module_from_spec(spec)
spec.loader.exec_module(replay)


def test_replay_order_follows_frozen_learning_tasks_and_repetitions():
    protocol = {"learn_ids": ["one", "two"], "parent": "p"}
    result = {"candidate": "c", "learning_replay": [{"task_id": "two", "run_id": "c2"},
              {"task_id": "one", "run_id": "c1"}], "learning": [
              {"task_id": "one", "run_id": "p1b", "repeat": 1},
              {"task_id": "two", "run_id": "p2", "repeat": 0},
              {"task_id": "one", "run_id": "p1", "repeat": 0}]}
    ordered = list(replay.eligible_rows(protocol, result))
    assert [row["run_id"] for row, *_ in ordered] == ["c1", "c2", "p1", "p1b", "p2"]
    assert [purpose for _, _, _, purpose in ordered] == ["evaluate", "evaluate", "solve", "solve", "solve"]


def test_replay_rejects_cross_split_source(tmp_path):
    with pytest.raises(ValueError, match="outside the frozen learning set"):
        list(replay.eligible_rows({"learn_ids": ["learn"], "parent": "p"}, {
            "candidate": "c", "learning_replay": [{"task_id": "dev"}], "learning": []}))
    store = replay.Archive(tmp_path / "formal.db")
    with pytest.raises(ValueError, match="cannot change"):
        with store.connect(write=True):
            pass
    assert not (tmp_path / "formal.db").exists()
