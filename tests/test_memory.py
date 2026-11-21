import pytest

from evocoder.contracts import canonical, digest
from evocoder.dataset import fixture_task
from evocoder.memory import freeze_pool, retrieve


def record(store, task, run_id, *, purpose="solve", mode="mock"):
    with store.connect(write=True) as db:
        db.execute(
            "INSERT INTO runs VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                run_id,
                task.id,
                task.split,
                digest(task.model_dump()),
                None,
                mode,
                purpose,
                "COMPLETE",
                0,
                canonical({"passed": True, "public_failed": False}),
                "{}",
            ),
        )
    store.event(run_id, "START", {"task": task.public()})
    store.event(
        run_id,
        "OBSERVATION",
        {"action": {"tool": "test"}, "observation": {"exit_code": 1, "output": "public failure"}},
    )
    store.event(run_id, "PATCH", {"diff": "public historical patch"})
    store.event(run_id, "GRADE", {"private": "secret-grader-string", "passed": True})


def test_retrieval_is_frozen_public_only_and_fits_context_budget(settings, store):
    old = fixture_task("old", "learn", False)
    record(store, old, "r")
    pool = freeze_pool(store, ["r"], "mock")
    new = fixture_task("new", "test", False)
    result = retrieve(store, pool, new, "mock", 2048)
    assert result["documents"][0]["run_id"] == "r"
    assert "secret-grader-string" not in canonical(result)
    assert result["bytes"] <= 2048
    assert retrieve(store, pool, new, "mock", 30)["documents"] == []
    store.event("r", "PATCH", {"diff": "changed public history"})
    with pytest.raises(ValueError, match="differs from its public execution"):
        retrieve(store, pool, new, "mock", 2048)


@pytest.mark.parametrize(
    "split,purpose,mode",
    [
        ("test", "solve", "mock"),
        ("dev", "solve", "mock"),
        ("learn", "evaluate", "mock"),
        ("learn", "solve", "live"),
    ],
)
def test_final_gate_or_other_mode_runs_cannot_become_rag_evidence(settings, store, split, purpose, mode):
    record(store, fixture_task("blocked", split, False), "r", purpose=purpose, mode=mode)
    with pytest.raises(ValueError, match="learn/solve"):
        freeze_pool(store, ["r"], "mock")
