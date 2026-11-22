import importlib
from pathlib import Path

import pytest

from evocoder.contracts import LibraryRoute, Skill, digest
from evocoder.errors import RoutingRejected
from evocoder.library import SkillLibrary
from evocoder.routing import library_task_view, select_library_skill
from test_library import seed, fixture_task


@pytest.fixture
def protocol(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).parents[1] / "scripts"))
    return importlib.import_module("portfolio_routing")


def test_parallel_pool_preserves_lineage_and_other_families(settings, store):
    root = seed(settings, store, fixture_task("first", "learn", False))
    other = seed(settings, store, fixture_task("other", "learn", False))
    child = seed(settings, store, fixture_task("version", "learn", True), root)
    sibling = seed(settings, store, fixture_task("alternative", "learn", True), root)
    lib = SkillLibrary(settings, store)
    assert lib.retain_parent([other, root], child) == sorted([root, child, other])
    assert lib.retain_parent([], child) == sorted([root, child])
    with pytest.raises(ValueError, match="direct parent"):
        lib.retain_parent([child], sibling)
    with pytest.raises(ValueError, match="direct parent"):
        lib.validate_family_pool([child, sibling])
    with pytest.raises(ValueError, match="direct parent"):
        lib.retain_parent([], root)


def test_public_preview_only_contains_bounded_public_excerpts(protocol, settings, store):
    _, (task, _) = protocol.cases()[2]
    root = seed(settings, store, fixture_task("r", "learn", False))
    skill = Skill.model_validate(store.get_artifact(root))
    noisy = task.model_copy(
        update={
            "files": {**task.files, task.editable[0]: "é" * 900 + "DISCARDED"},
            "grader_files": {"private.py": "hidden-only"},
        }
    )
    view = library_task_view(noisy, "public_preview")
    assert set(view) == {"issue", "public_excerpts"}
    assert [x["path"] for x in view["public_excerpts"]] == ["README.md", *task.editable]
    assert len(view["public_excerpts"][1]["content"].encode()) == 1200
    assert "private.py" not in str(view) and "DISCARDED" not in str(view)

    class Router:
        def __init__(self, quote):
            self.quote = quote

        def request(self, schema, prompt, data, run):
            assert schema is LibraryRoute
            assert set(data["skills"][0]) == {"id", "applicability", "exclusions"}
            return LibraryRoute(
                decision="apply",
                skill_id=root,
                condition_indices=[0],
                exclusion_indices=[],
                task_quotes=[self.quote],
                reason="Checked an excerpt.",
            )

    q = "boolean value is false"
    assert select_library_skill(Router(q), noisy, {root: skill}, "r", context="public_preview")["applied"]
    for context, quote in [("issue", q), ("public_preview", "hidden-only"), ("public_preview", "README.md")]:
        with pytest.raises(RoutingRejected, match="literal"):
            select_library_skill(Router(quote), noisy, {root: skill}, "r", context=context)


def test_route_context_is_part_of_published_library_identity(settings, store):
    root = seed(settings, store, fixture_task("replay", "learn", False))
    for context in ("issue", "public_preview", "invalid"):
        body = {
            "kind": "skill_library",
            "schema": 2,
            "mode": "mock",
            "members": [root],
            "route_context": context,
        }
        key = store.artifact("skill_library", body)
        if context == "invalid":
            with pytest.raises(ValueError, match="manifest"):
                store.library_members(key, mode="mock")
        else:
            assert store.library_members(key, mode="mock") == [root]
            with pytest.raises(ValueError, match="published"):
                store.library_members(key, mode="mock", require_published=True)
    assert digest(settings.public()) != digest(
        settings.model_copy(update={"library_route_context": "public_preview"}).public()
    )


def test_registered_diagnostic_selects_only_on_conservative_learn_scores(protocol):
    targets = {"c": "parent", "e": "child", "n": "skip"}
    rows = [
        {"task": t, "pool": "portfolio", "view": v, "decision": "skip", "selected": None}
        for t in targets
        for v in protocol.VIEWS
    ]
    assert protocol.selection(rows, targets)["view"] == "issue"
    rows[3].update(selected=protocol.CHILD, decision="apply")
    assert protocol.selection(rows, targets)["view"] == "public_preview"
    rows[-1].update(selected=protocol.PARENT, decision="apply")
    assert protocol.selection(rows, targets)["view"] == "issue"
    rows[-1].update(selected=None, decision="skip_invalid")
    assert protocol.selection(rows, targets)["view"] == "issue"
    rows[-1].update(decision="skip")
    rows[0].update(selected=protocol.PARENT, decision="apply")
    assert protocol.selection(rows, targets)["view"] == "issue"


@pytest.mark.docker
def test_published_parallel_library_routes_one_skill_or_skips(settings, store):
    from evocoder.contracts import Action
    from evocoder.runtime import Runner
    from test_library import GradeRunner

    support = [fixture_task(t, "learn", False) for t in ("branch-root", "branch-child")]
    parent = seed(settings, store, support[0])
    child = seed(settings, store, support[1], parent)
    dev = [fixture_task(x, "dev", False) for x in ("da-alpha", "db-beta")]
    preview = settings.model_copy(update={"library_route_context": "public_preview"})
    library = SkillLibrary(preview, store, GradeRunner(store, child, child))
    report = library.evaluate(child, dev, support, retain_parent=True)
    assert report["gate"]["passed"] and report["members"] == sorted([parent, child])
    store.activate_library(report["library_id"], report["receipt"], expected=None, mode="mock")

    class Chooser:
        def __init__(self, choice):
            self.choice = choice

        def request(self, schema, prompt, data, rid):
            if schema is LibraryRoute:
                assert "public_excerpts" in data["task"] and len(data["skills"]) == 2
                return LibraryRoute(
                    decision="apply" if self.choice else "skip",
                    skill_id=self.choice,
                    condition_indices=[0] if self.choice else [],
                    exclusion_indices=[],
                    task_quotes=[data["task"]["issue"]] if self.choice else [],
                    reason="controlled choice",
                )
            assert data["skills"] == ([store.get_artifact(self.choice)] if self.choice else [])
            assert data["remaining_steps"] == settings.max_steps - 1
            return Action(tool="finish")

    for key in (parent, child, None):
        rid = Runner(settings, store, Chooser(key)).solve(
            dev[0], use_active=False, library_id=report["library_id"]
        )
        run = store.run(rid)
        assert run["state"] in {"COMPLETE", "FAILED"} and run["skill_id"] == key
        assert run["result"]["routing"]["decision"] == ("apply" if key else "skip")
