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
