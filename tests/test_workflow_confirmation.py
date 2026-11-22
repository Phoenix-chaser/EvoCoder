import sys
from pathlib import Path

import pytest

from evocoder.contracts import digest

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
from workflow_confirmation import FREEZE, LOCK, PUBLIC, RESULT, diversity, matrix, observed_veto, read  # noqa: E402
from workflow_gate import schedule  # noqa: E402
from report_workflow_revision import panel_rows  # noqa: E402


def test_new_release_receipt_cannot_recycle_previous_configuration_arms():
    p, h, lock, original = read(PUBLIC), read(FREEZE), read(LOCK), read(RESULT)
    source = panel_rows(original["rows"], h)
    blocks = []
    for n in range(7):
        by = {(r["task"], a): r[a] for r in source if r["repeat"] == n for a in h["arms"][r["task"]]}
        seq = [job for job in schedule(p, h, n) if job[0] in h["config"]]
        blocks.append(
            dict(confirmation=digest(lock), round=n, attempts=[[by[job] for job in seq]], chosen=0, errors={})
        )
    with pytest.raises(ValueError, match="foreign or reused"):
        matrix(p, h, original, blocks, lock)


def test_observed_harm_from_original_config_cannot_be_erased_by_new_collection():
    p, h, original = read(PUBLIC), read(FREEZE), read(RESULT)
    rows = panel_rows(original["rows"], h)
    assert observed_veto(p, h, rows, h["child"]) == []
    task = p["config"][1]
    for row in rows:
        if row["task"] == task:
            row["revised"] = {**row["revised"], "passed": False, "first_solver_request": "different"}
            row["parent"] = {**row["parent"], "passed": True, "first_solver_request": "original"}
    assert [task, "parent"] in observed_veto(p, h, rows, h["child"])


def test_confirmation_holdout_requires_its_own_activation_and_complete_strata():
    p = read(PUBLIC)
    assert diversity(p, None, []) == ([], [])
    with pytest.raises(ValueError, match="without separate live receipt"):
        diversity(p, None, [{"family": "scalar", "rows": []}])
    with pytest.raises(ValueError, match="incomplete test strata"):
        diversity(p, {"protocol": "hypothetical-test-fixture"}, [])
