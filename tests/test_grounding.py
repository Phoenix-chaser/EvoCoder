import difflib
import json

import pytest

from evocoder.contracts import Settings, Task, canonical, digest
from evocoder.evolution import Evolution
from evocoder.grounding import build_grounding, render_summary, validate_summary
from evocoder.store import Store


@pytest.fixture
def recorded(tmp_path):
    # The public reproduction sees Command; the independently graded task fails.
    before = "class Command:\n    def help(self):\n        return ' old'\n\nclass Option:\n    def help(self):\n        return ' old'\n"
    task = Task(
        id="render-help", repository="test", source_group="help", split="learn",
        issue="Fix help spacing.", files={"ui.py": before}, editable=["ui.py"],
        public_command=["python", "public.py"], grader_files={"private_probe.py": "SECRET_FIXTURE = 3"},
        grader_command=["python", "private_probe.py"],
    )
    settings = Settings(_env_file=None, state_dir=tmp_path / "state")
    store = Store(settings.state_dir)
    with store.connect(write=True) as db:
        db.execute("INSERT INTO runs VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                   ("r1", task.id, "learn", digest(task.model_dump()), None,
                    "mock", "solve", "RUNNING", 0, None, "{}"))
    store.event("r1", "START", {"task": task.public()})
    after = before.replace("' old'", "'old'", 1)
    patch_hash = store.artifact("patch", {"ui.py": after})
    diff = "".join(difflib.unified_diff(before.splitlines(True), after.splitlines(True),
                                        fromfile="a/ui.py", tofile="b/ui.py"))
    store.event("r1", "PATCH", {"artifact": patch_hash, "diff": diff})
    store.event("r1", "PUBLIC_VERIFY", {
        "action": {"tool": "test", "argv": task.public_command},
        "observation": {"exit_code": 0, "timeout": False, "image_id": "image"},
    })
    store.event("r1", "GRADE", {"passed": False, "timeout": False, "image_id": "image",
                               "grader_hash": digest({"files": task.grader_files, "command": task.grader_command})})
    store.finish("r1", "FAILED", {"patch": patch_hash, "passed": False, "public_failed": False, "image_id": "image"})
    evidence = Evolution(settings, store).freeze(["r1"])[1]
    return task, store, evidence


def test_submitted_source_and_tests_determine_summary(recorded):
    task, store, evidence = recorded
    catalog = build_grounding(store, evidence, {task.id: task})
    receipt = catalog["receipts"]["r1"]
    assert receipt["files"][0]["symbols"] == ["Command.help"]
    assert receipt["public_check"] == {"scope": "submitted", "status": "passed", "seq": 3}
    assert receipt["final_grade"] == {"status": "failed", "seq": 4}
    assert "SECRET_FIXTURE" not in canonical(catalog) and "private_probe.py" not in canonical(catalog)
    summary = render_summary(catalog, ["r1"])
    validate_summary(summary, ["r1"], catalog)
    for false_summary in [summary.replace("Command.help", "Option.help"),
                          summary.replace("final grade failed", "final grade passed"),
                          summary + " Both output surfaces were fixed."]:
        with pytest.raises(ValueError, match="ungrounded"):
            validate_summary(false_summary, ["r1"], catalog)
    for bad_refs in [["r1","r1"], ["r1","not-visible"], []]:
        with pytest.raises(ValueError, match="receipts"):
            validate_summary(summary, bad_refs, catalog)


def test_snapshot_or_event_substitution_blocks_receipt(recorded):
    task, store, evidence = recorded
    wrong = task.model_copy(update={"files": {"ui.py": "class Replaced: pass"}})
    with pytest.raises(ValueError, match="snapshot"):
        build_grounding(store, evidence, {task.id: wrong})
    event = evidence["episodes"][0]["events"][1]
    event["payload"]["diff"] = "fabricated method"
    with pytest.raises(ValueError, match="frozen evidence"):
        build_grounding(store, evidence, {task.id: task})
    with store.connect(write=True) as db:
        db.execute("UPDATE events SET payload=? WHERE run_id='r1' AND seq=2", (canonical(event["payload"]),))
    with pytest.raises(ValueError, match="diff disagrees"):
        build_grounding(store, evidence, {task.id: task})


def test_final_grade_identity_is_checked(recorded):
    task, store, evidence = recorded
    grade = store.events("r1")[-1]["payload"]
    grade["grader_hash"] = "different-grader"
    with store.connect(write=True) as db:
        db.execute("UPDATE events SET payload=? WHERE run_id='r1' AND seq=4", (canonical(grade),))
    with pytest.raises(ValueError, match="final grade"):
        build_grounding(store, evidence, {task.id: task})


def test_exhaustion_records_workspace_test_separately(recorded):
    task, store, evidence = recorded
    with store.connect(write=True) as db:
        db.execute("DELETE FROM events WHERE seq > 1")
    store.event("r1", "OBSERVATION", {"action": {"tool": "test"}, "observation": {"exit_code": 1}})
    store.finish("r1", "EXHAUSTED", {"patch": None, "passed": False, "public_failed": True})
    evidence["episodes"][0].update(events=store.events("r1"), public_failed=True)
    catalog = build_grounding(store, evidence, {task.id: task})
    summary = render_summary(catalog, ["r1"])
    assert "no submitted patch" in summary and "last workspace public test failed" in summary
    assert "final grade not_run" in summary


@pytest.mark.parametrize("policy", [None, "grounded_scope"])
def test_grounded_revision_requires_receipts_and_rejects_external_summary(recorded, policy):
    from evocoder.evolution import RECEIPT_MARKER
    from evocoder.model import Model

    task, store, _ = recorded
    settings = Settings(_env_file=None, state_dir=store.root)
    parent = Evolution(settings, store).propose(["r1"])
    evolution = Evolution(settings, store, proposal_policy=policy)
    with store.connect(write=True) as db:
        db.execute("INSERT INTO runs VALUES(?,?,?,?,?,?,?,?,?,?,?)", (
            "r2", task.id, "learn", digest(task.model_dump()), parent, "mock", "solve", "EXHAUSTED",
            1, canonical({"patch": None, "passed": False, "public_failed": True}), "{}",
        ))
    store.event("r2", "START", {"task": task.public()})
    store.event("r2", "OBSERVATION", {"action": {"tool": "test"}, "observation": {"exit_code": 1}})
    with pytest.raises(ValueError, match="snapshot unavailable"):
        evolution.propose(["r1", "r2"], parent)

    store.artifact("learn_task_snapshot", task.model_dump())
    child = evolution.propose(["r1", "r2"], parent)
    with store.connect() as db:
        events = [json.loads(row[0]) for row in db.execute("SELECT payload FROM events WHERE kind='SUMMARY_VERIFIED'")]
    assert len(events) == 1 and events[0]["candidate"] == child
    receipt = store.get_artifact(events[0]["artifact"])
    assert store.get_artifact(receipt["proposal_output"])["change_summary"] == RECEIPT_MARKER
    validate_summary(store.get_artifact(child)["change_summary"], receipt["evidence_refs"],
                     store.get_artifact(receipt["catalog"]))

    class FabricatingProposer:
        def request(self, schema, prompt, data, run_id):
            return Model.mock_skill(data).model_copy(update={"change_summary": "All methods were repaired."})

    with pytest.raises(ValueError, match="rejected free-text summary"):
        Evolution(settings, store, FabricatingProposer(), proposal_policy=policy).propose(["r1", "r2"], parent)
