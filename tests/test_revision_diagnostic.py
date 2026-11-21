import pytest

from evocoder import revision_diagnostic as diagnostic
from evocoder.contracts import MODEL_TASK_VIEW, canonical, digest
from evocoder.dataset import fixture_task
from evocoder.gate import gate_summary
from evocoder.store import uid
from test_library import seed


@pytest.mark.parametrize("variant", ["skipped", "no_win", "win"])
def test_only_a_failure_with_a_real_parent_application_enters_the_diagnostic(
    settings, store, tmp_path, monkeypatch, variant,
):
    applied = variant != "skipped"
    root = fixture_task("seed", "learn", False)
    parent = seed(settings, store, root)
    follow_up = [fixture_task("first", "learn", True), fixture_task("second", "learn", True)]
    dev = [fixture_task("dev-1", "dev", True), fixture_task("dev-2", "dev", True)]
    holdout = [fixture_task("test-1", "test", True), fixture_task("test-2", "test", True)]
    tasks = {task.id: task for task in [root, *follow_up, *dev, *holdout]}
    with store.connect() as db:
        seed_run = db.execute("SELECT id FROM runs").fetchone()[0]
    protocol = {
        "parent": parent, "feedback_policy": "learn_outcome",
        "follow_up_tasks": [task.id for task in follow_up], "dev_tasks": [task.id for task in dev],
        "seed_evidence_runs": [seed_run],
        "test_tasks": [task.id for task in holdout], "holdout_repeats": 2,
        "origin": str(tmp_path / "earlier"), "settings": {},
    }
    calls = []

    class ControlledRunner:
        def __init__(self, settings, store):
            self.store = store

        def solve(self, task, *, use_active, skill_ids, candidate=False, purpose="solve"):
            assert not use_active
            if task.split == "test":
                assert purpose == "evaluate" and not candidate
                with self.store.connect() as db:
                    exposed = db.execute(
                        "SELECT task_id FROM test_exposures WHERE task_id=?", (task.id,),
                    ).fetchone()
                    assert exposed is not None
            calls.append((task.id, purpose, skill_ids))
            key = uid()
            skill_used = skill_ids[0] if purpose == "evaluate" or applied else None
            passed = purpose == "evaluate" and (
                task.split == "learn" or variant == "no_win" or skill_used != parent
            )
            outcome = {
                "passed": passed,
                "public_failed": not passed,
                "routing": {"decision": "apply" if skill_used else "skip"},
                "image_id": "sha256:test",
            }
            with self.store.connect(write=True) as db:
                db.execute("INSERT INTO runs VALUES(?,?,?,?,?,?,?,?,?,?,?)", (
                    key, task.id, task.split, digest(task.model_dump()), skill_used,
                    "mock", purpose, "COMPLETE" if passed else "FAILED", 0, canonical(outcome), "{}",
                ))
            self.store.event(key, "START", {"task": task.public(), "model_task_view": MODEL_TASK_VIEW})
            self.store.event(key, "OBSERVATION", {
                "action": {"tool": "edit"}, "observation": {"exit_code": 0},
            })
            self.store.event(key, "OBSERVATION", {
                "action": {"tool": "test"}, "observation": {"exit_code": int(not passed)},
            })
            return key

    monkeypatch.setattr(diagnostic, "Runner", ControlledRunner)
    result = {
        "follow_up": [], "candidate": None, "diagnostic": None, "proposal_attempts": 0,
        "holdout": {"status": "pending_gate", "rows": []},
    }
    outcome = diagnostic.collect_feedback(settings, store, tasks, protocol, tmp_path, result)
    assert calls[:2] == [("first", "solve", [parent]), ("second", "solve", [parent])]
    assert [trial["eligible_for_revision"] for trial in result["follow_up"]] == [applied, applied]
    assert not store.active_library()
    if applied:
        assert diagnostic.retained_feedback(settings, store, tasks, protocol, result) == result["follow_up"]
        tampered = {**result, "follow_up": [{**row, "eligible_for_revision": False} for row in result["follow_up"]]}
        with pytest.raises(ValueError, match="differs from the recorded learning execution"):
            diagnostic.retained_feedback(settings, store, tasks, protocol, tampered)
        assert outcome == "paired_diagnostic_completed"
        assert result["proposal_attempts"] == 1
        assert result["diagnostic"]["gate"]["passed"] == (variant == "win")
        assert result["diagnostic"]["receipt"] is None
        child = store.get_artifact(result["candidate"])
        assert child["parent"] == parent
        assert set(child["evidence_refs"]) == {seed_run, *(x["run_id"] for x in result["follow_up"])}
    else:
        assert outcome == "no_qualifying_parent_failure"
        assert result["proposal_attempts"] == 0 and len(calls) == 2

    diagnostics_count = len(calls)
    diagnostic.evaluate_holdouts(settings, store, tasks, protocol, tmp_path, result)
    summary = result["holdout"]
    with store.connect() as db:
        exposures = {r[0] for r in db.execute("SELECT task_id FROM test_exposures")}
    if variant == "win":
        child = result["candidate"]
        assert exposures == {"test-1", "test-2"}
        assert summary["status"] == "complete"
        assert summary["wins"] == summary["candidate_applications"] == 4
        assert summary["parent_passes"] == summary["regressions"] == 0
        assert calls[diagnostics_count:] == [
            ("test-1", "evaluate", [parent]), ("test-1", "evaluate", [child]),
            ("test-2", "evaluate", [child]), ("test-2", "evaluate", [parent]),
            ("test-1", "evaluate", [child]), ("test-1", "evaluate", [parent]),
            ("test-2", "evaluate", [parent]), ("test-2", "evaluate", [child]),
        ]
    else:
        expected = "blocked_no_candidate" if variant == "skipped" else "blocked_by_dev_gate"
        assert summary == {"status": expected, "rows": []}
        assert not exposures and len(calls) == diagnostics_count


def test_holdout_audit_rejects_same_id_with_different_split_and_hash(store, tmp_path):
    held_out = fixture_task("seen", "test", True)
    earlier = fixture_task("seen", "learn", False)
    assert diagnostic.check_fresh_holdouts([held_out], tmp_path, store) == 1
    with store.connect(write=True) as db:
        db.execute("INSERT INTO runs VALUES(?,?,?,?,?,?,?,?,?,?,?)", (
            uid(), earlier.id, earlier.split, digest(earlier.model_dump()), None,
            "live", "solve", "COMPLETE", 0, "{}", "{}",
        ))
    with pytest.raises(ValueError, match="overlaps a prior run"):
        diagnostic.check_fresh_holdouts([held_out], tmp_path, store)


def test_known_regression_does_not_count_as_a_dev_win_but_can_block_the_gate():
    def rows(key, *, known=False, passed=True):
        return [
            {
                "split": "dev", "task_id": key, "source_group": key,
                "repeat": repeat, "baseline_pass": not passed, "candidate_pass": passed,
                "skill_behavior": "candidate_applied", "infrastructure_error": False,
                "evaluation_role": "known_regression" if known else "fresh",
            }
            for repeat in range(2)
        ]

    first, second = rows("fresh-a"), rows("fresh-b")
    known_win = rows("earlier", known=True)
    only_one_new_win = gate_summary(first + known_win, 2)
    assert not only_one_new_win["gate"]["passed"]
    assert only_one_new_win["repeat_confirmed_improved_groups"] == ["fresh-a"]
    assert gate_summary(first + second + known_win, 2)["gate"]["passed"]
    known_loss = rows("earlier", known=True, passed=False)
    blocked = gate_summary(first + second + known_loss, 2)
    assert not blocked["gate"]["passed"]
    assert blocked["regressions"] == ["earlier"]


def test_grounded_retry_freezes_controls_from_same_failed_application():
    result = {
        "protocol": {"parent": "root"}, "candidate": "old",
        "follow_up": [{"task_id": "pallets-click-29bacc773e", "run_id": "learn", "eligible_for_revision": True}],
    }
    receipt = {
        "schema": 1, "parent": "root", "old_candidate": "old", "source_result_hash": digest(result),
        "run_id": "learn", "false_summary_rejected": True, "canonical_accepted": True, "model_calls": 0,
    }
    assert diagnostic.check_grounding_receipt(receipt, result) == digest(receipt)
    for key, invalid in (
        ("schema", True), ("parent", "other"), ("old_candidate", "other"),
        ("source_result_hash", "changed"), ("run_id", "dev"), ("model_calls", False),
        ("model_calls", 1), ("false_summary_rejected", False), ("canonical_accepted", False),
    ):
        with pytest.raises(ValueError, match="Click #3509"):
            diagnostic.check_grounding_receipt({**receipt, key: invalid}, result)


def test_retry_sentinels_remain_frozen_and_outside_improvement_credit():
    older, latest, fresh_a, fresh_b = [
        fixture_task(name, "dev", True) for name in ["old-regression", "new-regression", "fresh-a", "fresh-b"]
    ]
    prior = {"dev_tasks": [latest.id], "regression_tasks": [older.id]}
    result = {"diagnostic": {"regressions": [latest.id]}}
    original = diagnostic.task_fingerprints([older, latest])
    catalog = [older, latest, fresh_a, fresh_b]
    known, fresh = diagnostic.split_retry_dev(catalog, prior, result, original)
    assert known == [older.id, latest.id] and fresh == [fresh_a, fresh_b]
    for invalid in (
        catalog[1:], [older, fresh_a, fresh_b],
        [older.model_copy(update={"issue": "changed"}), latest, fresh_a, fresh_b],
        [older, latest, fresh_a], [older, latest, fresh_a, fresh_b.model_copy(update={"source_group": fresh_a.source_group})],
    ):
        with pytest.raises(ValueError, match="preserve prior regressions"):
            diagnostic.split_retry_dev(invalid, prior, result, original)
