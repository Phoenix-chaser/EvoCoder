from copy import deepcopy
from pathlib import Path

import pytest

from evocoder.contracts import LibraryRoute, Settings, digest
from evocoder.dataset import fixture_task
from evocoder.errors import RoutingRejected
from evocoder.repeat_gate import repeated_gate
from evocoder.route_shape import public_test_shape
from evocoder.routing import library_task_view, select_library_skill
from evocoder.model import Model


def test_test_shape_uses_only_declared_public_tests_and_abstracts_literals():
    task = fixture_task("shapes", "learn", False)
    editable = task.editable[0]
    task = task.model_copy(
        update={
            "files": {
                editable: "SECRET_IMPLEMENTATION",
                "README.md": "SECRET_DOC",
                "tests/t.py": "# SECRET_COMMENT\nclass E(Exception):\n def __bool__(self): return False\n"
                'err=E("SECRET_MSG")\nassert check({"SECRET_KEY": None}, err) is err\n',
                "tests/test_spare.py": "assert not UNUSED",
            },
            "public_command": ["python", "-B", "tests/t.py"],
            "grader_files": {"hidden.py": "HIDDEN_GRADER"},
        }
    )
    view = library_task_view(task, "public_shape")
    assert set(view) == {"issue", "public_test_shape"}
    signals = view["public_test_shape"]
    assert "assert Is: call:check / name:err" in signals
    assert "call check(dict[null], name:err)" in signals and "declared method: __bool__" in signals
    assert not any(s in str(signals) for s in ("SECRET", "UNUSED", "HIDDEN", "return False"))
    assert (
        Settings(_env_file=None, library_route_context="public_shape").public()["library_route_context"]
        == "public_shape"
    )
    for argv in (["python", "-c", "never"], ["executable", "tests/t.py"], ["python", editable]):
        assert public_test_shape(task.model_copy(update={"public_command": argv})) == [
            "editable file: " + Path(editable).name
        ]


def test_shape_quotes_are_grounded_and_decision_still_belongs_to_the_model():
    task = fixture_task("sources", "dev", False)
    skill = Model.mock_skill(
        {"parent": None, "evidence": {"episodes": [{"run_id": "unit", "public_failed": False}]}}
    )

    class Router:
        def __init__(self, q):
            self.q = q

        def request(self, schema, prompt, data, rid):
            assert schema is LibraryRoute and "public_test_shape" in data["task"]
            return LibraryRoute(
                decision="apply",
                skill_id="candidate",
                condition_indices=[0],
                exclusion_indices=[],
                task_quotes=[self.q],
                reason="grounded decision",
            )

    quote = public_test_shape(task)[0]
    assert select_library_skill(Router(quote), task, {"candidate": skill}, "r", context="public_shape")[
        "applied"
    ]
    with pytest.raises(RoutingRejected, match="literal"):
        select_library_skill(
            Router("unobserved token"), task, {"candidate": skill}, "r", context="public_shape"
        )


def panel():
    rows = []
    expected = {}
    for t, split, n in (("a", "dev", 5), ("b", "dev", 5), ("old", "learn", 3)):
        expected[t] = (split, "source-" + t, digest(t))
        for r in range(n):
            win = t != "old" and r < 3
            rows.append(
                dict(
                    task_id=t,
                    source_group="source-" + t,
                    split=split,
                    task_hash=digest(t),
                    repeat=r,
                    baseline_run=f"{t}-b-{r}",
                    candidate_run=f"{t}-c-{r}",
                    baseline_pass=not win,
                    candidate_pass=True,
                    baseline_applied_skill=None,
                    candidate_applied_skill="child" if win else None,
                    baseline_request=digest("b"),
                    candidate_request=digest("c") if win else digest("b"),
                    skill_behavior="candidate_applied" if win else "neither_applied",
                    infrastructure_error=False,
                )
            )
    return rows, expected


def test_five_round_threshold_counts_ties_and_reports_identical_request_noise_symmetrically():
    rows, expected = panel()
    rows[-1]["candidate_pass"] = False
    receipt = repeated_gate(rows, expected)
    assert receipt["gate"]["passed"] and receipt["tasks"]["a"]["attributed_wins"] == 3
    assert receipt["tasks"]["old"]["same_losses"] == 1 and receipt["tasks"]["old"]["changed_net"] == 0
    rows[-2]["baseline_pass"] = False
    assert repeated_gate(rows, expected)["tasks"]["old"]["same_wins"] == 1
    rows[0].update(
        candidate_request=digest("b"), candidate_applied_skill=None, skill_behavior="neither_applied"
    )
    assert not repeated_gate(rows, expected)["gate"]["passed"]


def test_protected_regressions_and_missing_or_duplicate_rounds_fail_closed():
    rows, expected = panel()
    rows[-1].update(candidate_pass=False, candidate_request=digest("c"), candidate_applied_skill="child")
    assert repeated_gate(rows, expected)["regressions"] == ["old"]
    rows, expected = panel()
    for r in rows[-2:]:
        r["candidate_pass"] = False
    assert repeated_gate(rows, expected)["tasks"]["old"]["raw_majority_loss"]
    for bad in [rows[:-1], rows[:-1] + [deepcopy(rows[-2])], rows[5:]]:
        with pytest.raises(ValueError):
            repeated_gate(bad, expected)


def test_repeat_evaluation_binds_real_entrypoint_to_all_rounds_and_equal_action_slots(settings, store):
    from test_budget_model import live_settings
    from test_library import GradeRunner, seed
    from evocoder.contracts import canonical
    from evocoder.library import SkillLibrary

    live = live_settings(settings).model_copy(update={"library_route_context": "public_shape"})
    ancestor = fixture_task("route-seed", "learn", False)
    skill = seed(live, store, ancestor)
    dev = [fixture_task("da-one", "dev", False), fixture_task("da-two", "dev", False)]

    class WireRunner(GradeRunner):
        def solve(self, task, *, use_active, purpose, skill_ids, reserve_empty_route=False):
            assert reserve_empty_route == (skill_ids == [])
            key = super().solve(task, use_active=use_active, purpose=purpose, skill_ids=skill_ids)
            result = self.store.run(key)["result"]
            result["routing"] = {"decision": "apply" if skill_ids else "none"}
            with self.store.connect(write=True) as db:
                db.execute("UPDATE runs SET result=?,mode='live' WHERE id=?", (canonical(result), key))
            self.store.event(
                key,
                "MODEL_RESPONSE",
                {"expected_function": "submit_action", "request_body_hash": digest([task.id, skill_ids])},
            )
            return key

    runner = WireRunner(store, skill)
    report = SkillLibrary(live, store, runner).evaluate(skill, dev, [ancestor], repeated=True)
    assert report["gate"]["passed"] and report["mode"] == "live"
    assert len(report["rows"]) == 2 * 5 + 3
    assert len(runner.calls) == 2 * len(report["rows"])
    store.activate_library(report["library_id"], report["receipt"], None, mode="live")
    assert store.get_artifact(store.active_library())["route_context"] == "public_shape"


def test_repeat_evaluation_requires_live_traces_and_matching_skill_identity(settings, store):
    from evocoder.library import SkillLibrary

    with pytest.raises(ValueError, match="wire receipts"):
        SkillLibrary(settings, store).evaluate("unused", [], [], repeated=True)
    rows, expected = panel()
    rows[0]["candidate_request"] = rows[0]["baseline_request"]
    with pytest.raises(ValueError, match="different skills"):
        repeated_gate(rows, expected)
    rows, expected = panel()
    rows[0]["infrastructure_error"] = True
    receipt = repeated_gate(rows, expected)
    assert not receipt["gate"]["passed"] and receipt["infrastructure_errors"] == ["a"]


@pytest.mark.docker
def test_bare_matched_slot_consumes_one_action_without_a_router_call(settings, store):
    from evocoder.contracts import Action
    from evocoder.runtime import Runner

    cfg = settings.model_copy(update={"max_steps": 3})
    task = fixture_task("slots", "dev", False)

    class Inspector:
        def request(self, schema, prompt, data, rid):
            assert schema is Action
            return Action(tool="read", path=task.editable[0], reason="Inspect public behavior")

    key = Runner(cfg, store, Inspector()).solve(
        task, use_active=False, purpose="evaluate", skill_ids=[], reserve_empty_route=True
    )
    result = store.run(key)
    assert result["state"] == "EXHAUSTED" and result["result"]["budget_steps"] == 3
    events = store.events(key)
    assert sum(e["kind"] == "ACTION" for e in events) == 2
    assert [e["payload"]["decision"] for e in events if e["kind"] == "SKILL_ROUTE"] == ["none"]


def test_release_needs_two_object_sources_and_also_protects_current_deployment(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts"))
    from run_shape_routing import CHILD, decisions

    p = {
        "objects": ["object-a", "object-b"],
        "config": ["config-a", "config-b"],
        "support": {"old": digest("old")},
    }
    rows, expected = [], {}
    for task in [*p["objects"], *p["config"], *p["support"]]:
        split = "learn" if task == "old" else "dev"
        expected[task] = (split, task, digest(task))
        for n in range(3 if split == "learn" else 5):
            r = dict(task=task, repeat=n, split=split, group=task, snapshot=digest(task))
            for name in ("bare", "parent", "issue_portfolio", "portfolio"):
                chosen = CHILD if name == "portfolio" and split == "dev" else None
                r[name] = dict(
                    run=f"{task}-{n}-{name}",
                    image="same-image",
                    passed=bool(chosen or n >= 3 or split == "learn"),
                    selected=chosen,
                    route="apply" if chosen else "none",
                    first_solver_request=digest([task, chosen]),
                )
            rows.append(r)
    outcomes, passed = decisions(p, rows, expected)
    assert passed and len(outcomes["objects_vs_parent"]["repeat_confirmed_improved_groups"]) == 2
    lost_objects = deepcopy(rows)
    for r in lost_objects:
        if r["task"] in p["objects"]:
            r["parent"]["passed"] = True
    outcomes, passed = decisions(p, lost_objects, expected)
    assert outcomes["whole_vs_parent"]["gate"]["passed"] and not passed
    for r in rows:
        if r["task"] == "old" and r["repeat"] < 2:
            r["parent"]["passed"] = r["portfolio"]["passed"] = False
    outcomes, passed = decisions(p, rows, expected)
    assert outcomes["whole_vs_parent"]["gate"]["passed"] and not passed
    assert outcomes["whole_vs_bare"]["tasks"]["old"]["raw_majority_loss"]


def test_public_study_recomputes_attribution_chronology_and_holdout_access(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts"))
    from report_shape_routing import PUBLIC, RESULT, read, verify

    p, original = read(PUBLIC), read(RESULT)
    assert verify(original, p)["holdout"] == original["holdout_exposures"]

    def fabricated_repeat(d):
        d["dev"][-1] = deepcopy(d["dev"][0])

    def fabricated_child_win(d):
        task = p["objects"][0]
        d["gate"]["decisions"]["objects_vs_parent"]["tasks"][task]["attributed_wins"] += 1

    def altered_wire(d):
        r = next(
            r
            for r in d["dev"]
            if r["portfolio"]["first_solver_request"] != r["parent"]["first_solver_request"]
        )
        r["portfolio"]["first_solver_request"] = r["parent"]["first_solver_request"]

    def crossed_phases(d):
        learning = d["billing"][d["routes"][0]["run"]]["study_calls"]
        development = d["billing"][d["dev"][0]["bare"]["run"]]["study_calls"]
        learning[0], development[0] = development[0], learning[0]

    def switched_exposure(d):
        d["holdout_exposures"] += 1

    def fabricated_release(d):
        d["published"] = not d["published"]

    def altered_router_request(d):
        d["wire_profile"]["temperature"] = 0.0

    def imputed_original(d):
        next(iter(d["partial_runs"].values()))["repair_grade"] = True

    def removed_reservation(d):
        key = next(iter(d["partial_runs"]))
        d["billing"][key]["reserved_nanos"] = 0

    def changed_intent(d):
        d["slot_options"]["portfolio"] = digest("another-model-configuration")

    for change in (
        fabricated_repeat,
        fabricated_child_win,
        altered_wire,
        crossed_phases,
        switched_exposure,
        fabricated_release,
        altered_router_request,
        imputed_original,
        removed_reservation,
        changed_intent,
    ):
        bad = deepcopy(original)
        change(bad)
        with pytest.raises(ValueError):
            verify(bad, p)
