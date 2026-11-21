from pathlib import Path

import pytest


@pytest.fixture
def audit(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).parents[1] / "scripts"))
    from audit_click_step_claim import TASK, classify, disabled_actual_line
    return TASK, classify, disabled_actual_line


def test_claim_cannot_join_states_from_distinct_tests(audit):
    task, classify, _ = audit
    base = {"task_id": task, "first_changed_public_check": True, "observed_edit_or_write_seqs": [12]}
    rows = [{**base, "named_states": {"enabled": "failed", "disabled": "failed"}, "disabled_actual_line": "E   + done=8"},
            {**base, "named_states": {"enabled": "passed", "disabled": "passed"}, "disabled_actual_line": None}]
    assert classify(rows) == ("unsupported_no_matching_invocation", [], [])


def test_matching_pair_requires_right_task_and_timing(audit):
    task, classify, _ = audit
    row = {"task_id": task, "first_changed_public_check": True, "observed_edit_or_write_seqs": [8],
           "named_states": {"enabled": "passed", "disabled": "failed"}, "disabled_actual_line": "E   + done=8"}
    assert classify([row]) == ("supported", [row], [row])
    assert classify([{**row, "task_id": "another"}])[0] == "unsupported_no_matching_invocation"
    assert classify([{**row, "observed_edit_or_write_seqs": [3, 8]}])[0] == "unsupported_timing"


def test_disabled_value_must_be_observed_in_its_own_failure(audit):
    _, _, extract = audit
    prefix = "_____ test_feature_enabled _____\nE   + done=8\n_____ test_feature_disabled _____\n"
    assert extract(prefix + "E   - done=8\nE   + done=11\n") is None
    assert extract(prefix + "E   - done=11\nE   + done=8\n") == "E   + done=8"
