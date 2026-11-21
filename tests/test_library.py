import json

import pytest

from evocoder.contracts import MODEL_TASK_VIEW, LibraryRoute, canonical, digest
from evocoder.dataset import fixture_task
from evocoder.errors import RoutingRejected
from evocoder.evolution import Evolution, ProposalModel
from evocoder.library import SkillLibrary
from evocoder.model import Model
from evocoder.routing import select_library_skill
from evocoder.runtime import Runner
from evocoder.store import uid


def seed(settings, store, task, parent=None):
    run_id = uid()
    with store.connect(write=True) as db:
        db.execute("INSERT INTO runs VALUES(?,?,?,?,?,?,?,?,?,?,?)", (
            run_id, task.id, task.split, digest(task.model_dump()), parent, settings.mode, "solve", "EXHAUSTED", 0,
            canonical({"public_failed": True, "passed": False, "patch": None}), "{}"))
    store.event(run_id, "START", {"task": task.public(), "model_task_view": MODEL_TASK_VIEW})
    store.event(run_id, "OBSERVATION", {"action": {"tool": "test"}, "observation": {"exit_code": 1}})
    evo = Evolution(settings, store, ProposalModel(settings.model_copy(update={"mode": "mock"}), store),
                    grounding_tasks={task.id: task})
    return evo.propose([run_id], parent)


@pytest.mark.parametrize("decision,selected,indices,quotes,valid", [
    ("apply", "second", [0], ["optional input"], True),
    ("skip", None, [], [], True), ("uncertain", None, [], [], True),
    ("apply", "invented", [0], ["optional input"], False),
    ("apply", "second", [8], ["optional input"], False),
    ("apply", "second", [0], ["not in the issue"], False),
    ("skip", "second", [], [], False),
])
def test_library_selection_is_grounded_and_does_not_see_procedures(decision, selected, indices, quotes, valid):
    skill = Model.mock_skill({"parent": None, "evidence": {"episodes": [
        {"run_id": "unit", "public_failed": False}]}})
    class Router:
        def request(self, schema, prompt, data, run_id):
            assert schema is LibraryRoute
            assert set(data["task"]) == {"issue"}
            assert all(set(s) == {"id", "applicability", "exclusions"} for s in data["skills"])
            return LibraryRoute(decision=decision, skill_id=selected, condition_indices=indices,
                                exclusion_indices=[], task_quotes=quotes, reason="Controlled selection")
    task = fixture_task("unit", "learn", False)
    if valid:
        result = select_library_skill(Router(), task, {"first": skill, "second": skill}, "r")
        assert result["applied"] is (decision == "apply")
    else:
        with pytest.raises(RoutingRejected):
            select_library_skill(Router(), task, {"first": skill, "second": skill}, "r")


def test_family_replacement_preserves_other_roots_and_forbids_downgrade(settings, store):
    a = seed(settings, store, fixture_task("a", "learn", False))
    b = seed(settings, store, fixture_task("b", "learn", False))
    a2 = seed(settings, store, fixture_task("a2", "learn", True), a)
    library = SkillLibrary(settings, store)
    assert library.replace([a, b], a2) == sorted([a2, b])
    with pytest.raises(ValueError, match="descend"):
        library.replace([a2, b], a)
    with pytest.raises(ValueError, match="multiple versions"):
        library.replace([a, a2], b)
    with pytest.raises(ValueError, match="same-mode"):
        from test_budget_model import live_settings
        SkillLibrary(live_settings(settings), store).provenance(a)


class GradeRunner:
    """Controlled scores for gate policy tests, not repair or model measurements."""
    def __init__(self, store, a, b=None, regress=False):
        self.store, self.a, self.b, self.regress, self.calls = store, a, b, regress, []

    def solve(self, task, *, use_active, purpose, skill_ids):
        assert not use_active and purpose == "evaluate"
        self.calls.append((task.id, list(skill_ids)))
        passed = (self.a in skill_ids if task.id.startswith("da") else
                  self.b in skill_ids if task.id.startswith("db") else True)
        if self.regress and self.b in skill_ids and task.id == self.regress:
            passed = False
        key = uid()
        relevant = self.a if task.id.startswith("da") else self.b if task.id.startswith("db") else None
        applied = relevant if relevant in skill_ids else skill_ids[0] if skill_ids else None
        with self.store.connect(write=True) as db:
            db.execute("INSERT INTO runs VALUES(?,?,?,?,?,?,?,?,?,?,?)", (
                key, task.id, task.split, digest(task.model_dump()), applied,
                "mock", purpose, "COMPLETE" if passed else "FAILED", 0,
                canonical({"passed": passed, "image_id": "sha256:controlled"}), "{}"))
        return key


def cases():
    return [fixture_task(name, "dev", False) for name in ["da1", "da2", "db1", "db2"]]


def test_unpublished_parent_and_child_can_be_compared_without_release_receipt(settings, store):
    first = fixture_task("seed", "learn", False)
    follow_up = fixture_task("follow-up", "learn", True)
    parent = seed(settings, store, first)
    child = seed(settings, store, follow_up, parent)
    runner = GradeRunner(store, parent, child)
    library = SkillLibrary(settings, store, runner)
    report = library.evaluate(child, cases()[2:], [first, follow_up], diagnostic_parent=parent)
    assert report["kind"] == "parent_child_diagnostic" and report["gate"]["passed"]
    assert report["baseline_members"] == [parent] and report["members"] == [child]
    assert report["library_id"] is None and report["receipt"] is None
    assert {row["baseline_applied_skill"] for row in report["rows"]} == {parent}
    assert {row["candidate_applied_skill"] for row in report["rows"]} == {child}
    with store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM library_evaluations").fetchone()[0] == 0
    assert store.active_library() is None


def test_solver_budget_failure_does_not_abort_the_whole_library_gate(settings, store):
    task = fixture_task("learn-budget", "learn", False)
    key = seed(settings, store, task)

    class LimitedRunner(GradeRunner):
        def solve(self, task, **kwargs):
            run_id = super().solve(task, **kwargs)
            if task.id == "da1":
                self.store.finish(run_id, "EXHAUSTED", {
                    "passed": False, "image_id": "sha256:controlled",
                    "exhaustion_reason": "solver_command_timeout",
                })
            return run_id

    runner = LimitedRunner(store, key)
    report = SkillLibrary(settings, store, runner).evaluate(key, cases(), [task])
    assert len(runner.calls) == 10 and len(report["rows"]) == 5
    assert not report["gate"]["passed"] and report["infrastructure_errors"] == []
    failed = next(row for row in report["rows"] if row["task_id"] == "da1")
    assert not failed["baseline_pass"] and not failed["candidate_pass"]
    assert store.active_library() is None


@pytest.mark.parametrize("regression_task", ["da1", "la"])
def test_whole_library_gate_blocks_interference_and_replays_both_families(settings, store, regression_task):
    ta, tb = fixture_task("la", "learn", False), fixture_task("lb", "learn", False)
    a, b = seed(settings, store, ta), seed(settings, store, tb)
    runner = GradeRunner(store, a, b)
    library = SkillLibrary(settings, store, runner)
    first = library.evaluate(a, cases(), [ta])
    assert first["gate"]["passed"]
    with pytest.raises(ValueError, match="not been published"):
        store.library_members(first["library_id"], mode="mock", require_published=True)
    store.activate_library(first["library_id"], first["receipt"], None, mode="mock")
    with pytest.raises(ValueError, match="current library mode"):
        store.activate_library(None, None, first["library_id"], mode="live", rollback=True)
    runner.regress = regression_task
    rejected = library.evaluate(b, cases(), [ta, tb])
    assert len(rejected["improved_groups"]) == 2 and rejected["regressions"] == [regression_task]
    assert not rejected["gate"]["passed"] and store.active_library() == first["library_id"]
    with pytest.raises(ValueError, match="passing receipt"):
        store.activate_library(rejected["library_id"], rejected["receipt"], first["library_id"], mode="mock")
    assert {c[0] for c in runner.calls} >= {"la", "lb"}
    runner.regress = False
    accepted = library.evaluate(b, cases(), [ta, tb])
    assert accepted["gate"]["passed"]
    store.activate_library(accepted["library_id"], accepted["receipt"], first["library_id"], mode="mock")
    assert store.library_members(store.active_library(), mode="mock", require_published=True) == sorted([a, b])
    with pytest.raises(ValueError, match="compare-and-swap"):
        store.activate_library(None, None, first["library_id"], mode="mock", rollback=True)
    store.activate_library(first["library_id"], None, accepted["library_id"], mode="mock", rollback=True)
    with pytest.raises(ValueError, match="stale"):
        store.activate_library(first["library_id"], first["receipt"], first["library_id"], mode="mock")
    store.activate_library(None, None, first["library_id"], mode="mock", rollback=True)
    assert store.active_library() is None and store.active() is None


def test_wins_when_both_runs_skip_candidate_cannot_publish(settings, store):
    task = fixture_task("seed", "learn", False)
    key = seed(settings, store, task)

    class BothSkip(GradeRunner):
        def solve(self, task, **kwargs):
            run_id = super().solve(task, **kwargs)
            with self.store.connect(write=True) as db:
                db.execute("UPDATE runs SET skill_id=NULL WHERE id=?", (run_id,))
            return run_id

    library = SkillLibrary(settings, store, BothSkip(store, key))
    report = library.evaluate(key, cases(), [task])
    assert len(report["improved_groups"]) == 2
    assert report["repeat_confirmed_improved_groups"] == []
    assert {r["task_id"] for r in report["wins_without_skill_change"]} == {"da1", "da2"}
    with pytest.raises(ValueError, match="passing receipt"):
        store.activate_library(report["library_id"], report["receipt"], None, mode="mock")


def test_semantic_skip_of_replaced_skill_must_win_in_every_repeat():
    from evocoder.gate import gate_summary, skill_behavior

    rows = []
    for task in ["one", "two"]:
        for repeat in (0, 1):
            row = {
                "task_id": task, "source_group": task, "split": "dev", "repeat": repeat,
                "baseline_applied_skill": "parent", "candidate_applied_skill": None,
                "candidate_route": "skip", "baseline_pass": False, "candidate_pass": True,
                "infrastructure_error": False,
            }
            row["skill_behavior"] = skill_behavior(row, "child", {"parent"})
            rows.append(row)
    assert gate_summary(rows, 2)["gate"]["passed"]
    rows[-1]["candidate_route"] = "skip_invalid"
    rows[-1]["skill_behavior"] = skill_behavior(rows[-1], "child", {"parent"})
    assert gate_summary(rows, 2)["repeat_confirmed_improved_groups"] == ["one"]
    assert not gate_summary(rows, 2)["gate"]["passed"]


@pytest.mark.parametrize("problem", ["missing", "changed", "overlap"])
def test_gate_rejects_missing_support_changed_snapshots_or_dev_leak_before_calls(settings, store, problem):
    task = fixture_task("learn", "learn", False)
    a = seed(settings, store, task)
    runner = GradeRunner(store, a)
    dev, support = cases(), [task]
    if problem == "missing":
        support = []
    elif problem == "changed":
        support = [task.model_copy(update={"issue": "different snapshot"})]
    else:
        dev[0] = dev[0].model_copy(update={"source_group": task.source_group})
    with pytest.raises(ValueError, match="support replay|overlaps"):
        SkillLibrary(settings, store, runner).evaluate(a, dev, support)
    assert runner.calls == []


def test_library_publication_is_separate_from_single_skill_receipts_and_mode(settings, store):
    task = fixture_task("learn", "learn", False)
    key = seed(settings, store, task)
    body = {"schema": 1, "kind": "skill_library", "mode": "mock", "members": [key]}
    library_id = store.artifact("skill_library", body)
    with pytest.raises(ValueError, match="passing receipt"):
        store.activate_library(library_id, "fake", None, mode="mock")
    with pytest.raises(ValueError, match="never published"):
        store.activate_library(library_id, None, None, mode="mock", rollback=True)
    with pytest.raises(ValueError, match="mode differs"):
        store.library_members(library_id, mode="live")
    with pytest.raises(ValueError, match="not been published"):
        Runner(settings, store).solve(task, use_active=False, library_id=library_id)
    with pytest.raises(ValueError, match="restricted"):
        Runner(settings, store).solve(fixture_task("test", "test", False), use_active=False,
                                      skill_ids=[key], candidate=True)


def test_library_model_contract_is_billed_and_invalid_response_is_not_applied(settings, store):
    import httpx
    from evocoder.errors import RoutingRejected
    from test_budget_model import live_settings
    def handler(request):
        body = json.loads(request.content)
        assert body["tool_choice"]["function"]["name"] == "select_library_skill"
        assert body["tools"][0]["function"]["parameters"]["properties"]["skill_index"]["enum"] == [-2, -1, 0]
        return httpx.Response(200, json={"usage": {"prompt_tokens": 100, "completion_tokens": 30}, "choices": [{
            "finish_reason": "tool_calls",
            "message": {"tool_calls": [{"function": {"name": "select_library_skill", "arguments": "{}"}}]}}]})
    model = Model(live_settings(settings), store, httpx.MockTransport(handler))
    with pytest.raises(RoutingRejected):
        model.request(LibraryRoute, "unit", {"skills": [{"id": "a"}]}, "r")
    assert store.costs(["r"])["calls"] == 1 and store.budget()["reserved_usd"] == 0


def test_gate_stops_after_first_execution_error_without_further_model_attempts(settings, store):
    task = fixture_task("learn", "learn", False)
    key = seed(settings, store, task)
    class BrokenRunner(GradeRunner):
        def solve(self, *args, **kwargs):
            run_id = super().solve(*args, **kwargs)
            store.finish(run_id, "ERROR", {"passed": False, "error": "ambiguous response"})
            return run_id
    runner = BrokenRunner(store, key)
    with pytest.raises(RuntimeError, match="stop without retry"):
        SkillLibrary(settings, store, runner).evaluate(key, cases(), [task])
    assert len(runner.calls) == 1
    with store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM library_evaluations").fetchone()[0] == 0


@pytest.mark.parametrize("route", ["uncertain", "skip_invalid"])
def test_uncertain_or_invalid_skip_cannot_start_an_unrelated_skill_branch(settings, store, tmp_path, monkeypatch, route):
    import importlib
    module = importlib.import_module("evocoder.library_stream")
    tasks = [fixture_task("seed", "learn", False), fixture_task("next", "learn", False), *cases()]
    key = seed(settings, store, tasks[0])
    root = tmp_path / "bundle"
    root.mkdir()
    entries = []
    for task in tasks:
        (root / f"{task.id}.json").write_text(canonical(task.model_dump()))
        entries.append({"path": f"{task.id}.json", "hash": digest(task.model_dump())})
    (root / "manifest.json").write_text(canonical({"tasks": entries}))
    class UncertainRunner:
        def __init__(self, *args):
            pass
        def solve(self, task, **kwargs):
            run_id = uid()
            with store.connect(write=True) as db:
                db.execute("INSERT INTO runs VALUES(?,?,?,?,?,?,?,?,?,?,?)", (
                    run_id, task.id, task.split, digest(task.model_dump()), None, settings.mode, "solve", "FAILED", 0,
                    canonical({"public_failed": True, "passed": False, "routing": {"decision": route}}), "{}"))
            store.event(run_id, "START", {"task": task.public()})
            return run_id
    monkeypatch.setattr(module, "Runner", UncertainRunner)
    report = module.library_stream(settings, store, root / "manifest.json", tmp_path / "report", initial_skills=[key])
    assert report["completed"] and report["proposal_count"] == 0
    assert report["pool"] == [key]
    assert all(r["decision"] == "uncertain_route_no_new_family" for r in report["timeline"])
