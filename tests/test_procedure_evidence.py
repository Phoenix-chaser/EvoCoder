import json

import pytest

from evocoder.contracts import Settings, Skill, Task, canonical, digest
from evocoder.evolution import RECEIPT_MARKER, Evolution
from evocoder.procedure_evidence import PUBLIC_CASE_FILE, behavior_receipt, proposal_view, step_edits
from evocoder.store import Store


@pytest.fixture
def trace(tmp_path):
    tests = "def test_feature_enabled(): pass\ndef test_feature_disabled(): pass\n"
    task = Task(id="learn-feature", source_group="g", repository="fixture", split="learn", issue="Missing text",
                files={"text.py": "x = 0", PUBLIC_CASE_FILE: tests}, editable=["text.py"],
                public_command=["python", "-m", "pytest", "-rA", PUBLIC_CASE_FILE],
                grader_files={"secret.py": "DO_NOT_TRANSMIT = 1"}, grader_command=["python", "secret.py"])
    settings = Settings(_env_file=None, state_dir=tmp_path)
    store = Store(tmp_path)
    parent = store.artifact("skill", Skill(name="parent-help", applicability=["CLI formatting"],
        steps=["Examine the affected output"], verification=["Check the public suite"], exclusions=["Parsing"],
        counterexamples=[], evidence_refs=["previous-seed"], change_summary="Parent procedure", parent=None).model_dump())
    with store.connect(write=True) as db:
        db.execute("INSERT INTO runs VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                   ("r", task.id, "learn", digest(task.model_dump()), parent, "mock", "solve", "EXHAUSTED", 1,
                    canonical({"patch": None, "passed": False, "public_failed": True, "image_id": "image"}), "{}"))
    store.event("r", "START", {"task": task.public()})
    changed = store.artifact("patch", {"text.py": "x = 2"})
    broken = store.artifact("patch", {"text.py": "x = 3"})
    stdout = f"PASSED {PUBLIC_CASE_FILE}::test_feature_enabled\nPASSED {PUBLIC_CASE_FILE}::test_feature_disabled\n"
    check = {"task_hash": digest(task.model_dump()), "patch_hash": changed, "model_step": 3,
             "argv": task.public_command, "image_id": "image", "exit_code": 0, "timeout": False,
             "changed_from_input": True, "output_hash": digest(stdout)}
    obs = {"output": stdout, "exit_code": 0}
    store.event("r", "CHECKPOINT_TESTED", check)
    store.event("r", "PUBLIC_CHECKPOINT", check)
    store.event("r", "OBSERVATION", {"action": {"tool": "test"}, "observation": {**obs, "saved_checkpoint": changed}})
    store.event("r", "OBSERVATION", {"action": {"tool": "command", "argv": ["echo", stdout]}, "observation": obs})
    store.event("r", "CHECKPOINT_RESTORED", {**check, "model_step": 5, "restored_patch_hash": changed,
                                           "previous_patch_hash": broken})
    store.event("r", "OBSERVATION", {"action": {"tool": "restore_checkpoint"},
                                     "observation": {**obs, "restored_checkpoint": changed}})
    return task, settings, store, parent


def test_checkpoint_and_named_test_evidence(trace):
    task, _, store, _ = trace
    receipt = behavior_receipt(store, task, "r")
    view = proposal_view(receipt)
    assert len(view["solver_requested_frozen_tests"]) == 1
    assert view["solver_requested_frozen_tests"][0]["named_states"] == {"enabled": "passed", "disabled": "passed"}
    assert len(view["saved_green"]) == len(view["restored_green"]) == 1
    assert view["restored_green"][0]["changed_workspace"]
    assert len(view["other_solver_command_sequences"]) == 1
    assert not view["submitted_patch_public_checks"]
    assert "DO_NOT_TRANSMIT" not in canonical(view)


@pytest.mark.parametrize("tamper", ["snapshot", "stdout", "restore_binding", "restore_output"])
def test_reject_fabricated_procedure_receipts(trace, tamper):
    task, _, store, _ = trace
    if tamper == "snapshot":
        task = task.model_copy(update={"split": "dev"})
    else:
        seq = {"stdout": 4, "restore_binding": 6, "restore_output": 7}[tamper]
        e = store.events("r")[seq-1]["payload"]
        if tamper == "restore_binding":
            e["restored_patch_hash"] = "other"
        else:
            e["observation"]["output"] = "fabricated"
        with store.connect(write=True) as db:
            db.execute("UPDATE events SET payload=? WHERE run_id='r' AND seq=?", (canonical(e), seq))
    with pytest.raises(ValueError):
        behavior_receipt(store, task, "r")


def test_only_traceable_failures_can_revise_ordered_steps(trace):
    task, settings, store, parent = trace

    class Proposer:
        calls = 0

        def request(self, schema, prompt, data, run):
            self.calls += 1
            assert set(data["observed_learning_procedures"]) == {"r"}
            assert "DO_NOT_TRANSMIT" not in canonical(data) and "secret.py" not in canonical(data)
            assert "steps" in prompt and data["allowed_evidence_refs"] == ["r"]
            old = data["parent"]["skill"].copy()
            old.update(parent=parent, evidence_refs=["r"], change_summary=RECEIPT_MARKER)
            if self.calls == 1:
                old["verification"] = ["Changed only outside the steps"]
            else:
                old["steps"] = [*old["steps"], "Check both states and keep the passing patch"]
            return schema.model_validate(old)

    model = Proposer()
    evolution = Evolution(settings, store, model, feedback_policy="learn_outcome",
                          proposal_policy="learn_procedure_feedback", grounding_tasks={task.id: task})
    with pytest.raises(ValueError, match="change the ordered steps"):
        evolution.propose(["r"], parent)
    key = evolution.propose(["r"], parent)
    c = store.get_artifact(key)
    with store.connect() as db:
        saved, = db.execute("SELECT payload FROM events WHERE kind='SUMMARY_VERIFIED'").fetchone()
    receipt = store.get_artifact(json.loads(saved)["artifact"])
    assert c["parent"] == parent and c["evidence_refs"] == ["r"]
    assert receipt["step_edits"] == step_edits(store.get_artifact(parent)["steps"], c["steps"])
    assert "verified restores: 1" in c["change_summary"]
    with store.connect(write=True) as db:
        db.execute("UPDATE runs SET skill_id=NULL WHERE id='r'")
    with pytest.raises(ValueError, match="parent"):
        evolution.propose(["r"], parent)
    assert model.calls == 2


def test_phase_and_behavior_are_independently_checked(trace):
    task, settings, store, parent = trace
    wrong = task.model_copy(update={"split": "dev"})
    with pytest.raises(ValueError, match="learning-only"):
        Evolution(settings, store, proposal_policy="learn_procedure_feedback",
                  grounding_tasks={task.id: wrong}).propose(["r"], parent)
