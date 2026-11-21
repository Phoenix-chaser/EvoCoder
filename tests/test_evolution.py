import json

import pytest

from evocoder.contracts import canonical
from evocoder.evolution import Evolution
from evocoder.model import Model


def record(store, run_id, split="learn", purpose="solve", parent=None, failed=False):
    with store.connect(write=True) as db:
        db.execute(
            "INSERT INTO runs VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                run_id,
                run_id,
                split,
                "taskhash",
                parent,
                "mock",
                purpose,
                "COMPLETE",
                0,
                canonical({"public_failed": failed, "passed": not failed}),
                "{}",
            ),
        )
    store.event(run_id, "START", {"task": {"id": run_id, "source_group": run_id, "issue": "public"}})
    store.event(run_id, "OBSERVATION", {"output": "public test failed" if failed else "OK"})
    store.event(run_id, "GRADE", {"passed": False, "private": "hidden evaluator detail"})


@pytest.mark.parametrize("split,purpose", [("test", "solve"), ("dev", "solve"), ("learn", "evaluate")])
def test_final_and_gate_feedback_cannot_become_learning_evidence(settings, store, split, purpose):
    record(store, "r", split=split, purpose=purpose)
    with pytest.raises(ValueError, match="dev/test/evaluator"):
        Evolution(settings, store).freeze(["r"])


def test_frozen_evidence_excludes_grade_and_is_immutable(settings, store):
    record(store, "r")
    key, evidence = Evolution(settings, store).freeze(["r"])
    assert "hidden evaluator" not in canonical(evidence)
    assert "GRADE" not in canonical(evidence)
    store.event("r", "OBSERVATION", {"later": "new evidence"})
    assert store.get_artifact(key) == evidence


def test_revision_requires_failure_from_actual_parent_application(settings, store):
    # Replay the previous prompt contract on the reduced historical records from record().
    evolution = Evolution(settings, store, proposal_policy="standard")
    record(store, "r")
    parent = evolution.propose(["r"])
    record(store, "other", parent="different", failed=True)
    with pytest.raises(ValueError, match="applying the parent"):
        evolution.propose(["other"], parent)
    record(store, "failure", parent=parent, failed=True)
    revision = evolution.propose(["r", "failure"], parent)
    assert store.get_artifact(revision)["counterexamples"]
    assert evolution.lineage()[1]["changes"]["steps"]


def test_failed_independent_learning_check_can_trigger_revision_without_grader_details(settings, store):
    record(store, "seed")
    evolution = Evolution(settings, store, feedback_policy="learn_outcome")
    parent = evolution.propose(["seed"])
    record(store, "applied", parent=parent)
    with store.connect(write=True) as db:
        db.execute(
            "UPDATE runs SET state='FAILED',result=? WHERE id='applied'",
            (canonical({"public_failed": False, "passed": False, "patch": "submitted"}),),
        )
    store.event("applied", "OBSERVATION", {"action": {"tool": "edit"}, "observation": {"exit_code": 0}})
    store.event("applied", "PUBLIC_VERIFY", {"observation": {"exit_code": 0}})
    store.event("applied", "GRADE", {"passed": False, "timeout": True})
    with pytest.raises(ValueError, match="qualifying execution feedback"):
        evolution.propose(["applied"], parent)
    store.event("applied", "GRADE", {"passed": False, "timeout": False, "private": "grading output marker"})
    with pytest.raises(ValueError, match="qualifying execution feedback"):
        Evolution(settings, store, feedback_policy="post_edit_or_budget").propose(["applied"], parent)

    class RecordingProposer:
        def request(self, schema, system, data, run_id):
            assert "grading output marker" not in canonical(data)
            assert "hidden evaluator detail" not in canonical(data)
            episode = data["evidence"]["episodes"][0]
            assert episode["execution_feedback"]["learn_submission_failed_independent_check"]
            assert episode["run_id"] == "applied"
            proposed = Model.mock_skill(data)
            return proposed.model_copy(update={"steps": ["Check an input boundary derived from the documented contract"]})

    candidate = Evolution(settings, store, RecordingProposer(), "learn_outcome",
                          proposal_policy="standard").propose(["applied"], parent)
    assert store.get_artifact(candidate)["parent"] == parent


def test_fabricated_citation_or_parent_cannot_be_saved(settings, store):
    record(store, "r")

    class Fabricator:
        def request(self, schema, system, data, run_id):
            result = Model.mock_skill(data)
            return result.model_copy(update={"evidence_refs": ["nonexistent"]})

    with pytest.raises(ValueError, match="outside frozen"):
        Evolution(settings, store, Fabricator()).propose(["r"])


def test_activation_receipt_and_compare_and_swap(store):
    with pytest.raises(ValueError, match="evaluator-issued"):
        store.activate("fake", "fabricated", expected=None)
    report = {"mode": "mock", "active_at_evaluation": None}
    with store.connect(write=True) as db:
        db.execute("INSERT INTO evaluations VALUES('receipt','good',NULL,?,1)", (canonical(report),))
    store.activate("good", "receipt", expected=None)
    with pytest.raises(ValueError, match="compare-and-swap"):
        store.activate(None, None, expected=None)
    with pytest.raises(ValueError, match="stale"):
        store.activate("good", "receipt", expected="good")
    store.activate(None, None, expected="good", reason="rollback")
    assert store.active() is None


def test_no_mock_to_live_evidence_conversion(settings, store):
    record(store, "r")
    with pytest.raises(ValueError, match="mixed"):
        Evolution(settings.model_copy(update={"mode": "live"}), store, object()).freeze(["r"])


def test_artifact_tamper_is_detected(store):
    key = store.artifact("test", {"a": 1})
    with store.connect(write=True) as db:
        db.execute("UPDATE artifacts SET body=? WHERE id=?", (json.dumps({"a": 2}), key))
    with pytest.raises(ValueError, match="integrity"):
        store.get_artifact(key)


def test_settled_invalid_live_proposal_is_typed_rejection_not_an_admitted_skill(settings, store):
    import httpx
    from evocoder.contracts import canonical
    from evocoder.errors import ProposalRejected
    from test_budget_model import live_settings

    record(store, "learn")
    with store.connect(write=True) as db:
        db.execute("UPDATE runs SET mode='live' WHERE id='learn'")
    body = {
        "usage": {"prompt_tokens": 100, "completion_tokens": 30},
        "choices": [
            {
                "finish_reason": "tool_calls",
                "message": {
                    "tool_calls": [
                        {"function": {"name": "submit_skill", "arguments": canonical({"name": "invalid"})}}
                    ]
                },
            }
        ],
    }
    options = live_settings(settings)
    model = Model(options, store, httpx.MockTransport(lambda request: httpx.Response(200, json=body)))
    with pytest.raises(ProposalRejected):
        Evolution(options, store, model).propose(["learn"])
    assert Evolution(options, store, model).lineage() == []
    assert store.budget()["reserved_usd"] == 0
