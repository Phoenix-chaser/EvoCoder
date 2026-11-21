import importlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest


def modules(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts"))
    return tuple(
        importlib.import_module(n)
        for n in ("falsy_transfer_protocol", "run_falsy_transfer", "falsy_transfer_cases")
    )


def pair(task, repeat, failed=()):
    probes = dict.fromkeys(("public", "controls", "first", "second"), True)
    return {
        "task": task,
        "repeat": repeat,
        "bare": {
            "patch": "submitted",
            "probes": probes | dict.fromkeys(failed, False),
            "passed": not failed,
            "applied": None,
        },
        "manual": {
            "patch": "reference-result",
            "probes": probes.copy(),
            "passed": True,
            "applied": "reference",
        },
    }


def test_repeated_rescue_requires_same_case_and_named_boundary(monkeypatch):
    protocol, _, _ = modules(monkeypatch)
    ids = ["cat", "dog"]
    rows = [pair(t, n) for n in range(3) for t in ids]
    rows[0] = pair("cat", 0, ("first",))
    rows[1] = pair("dog", 0, ("first",))
    rows[2] = pair("cat", 1, ("second",))
    assert not protocol.pilot_decision(rows, ids)["passed"]
    rows[4] = pair("cat", 2, ("first", "second"))
    assert protocol.pilot_decision(rows, ids) == {
        "passed": True,
        "repeated_rescues": {"cat": {"first": 2, "second": 2}},
    }
    with pytest.raises(ValueError, match="paired repetitions"):
        protocol.pilot_decision(rows[:-1], ids)


def test_public_failure_timeout_or_reference_skip_is_not_a_semantic_rescue(monkeypatch):
    protocol, _, _ = modules(monkeypatch)
    for r in (pair("x", 0, ("public", "first")), pair("x", 0, ("controls", "first"))):
        assert not protocol.rescue_targets(r, "manual")
    for arm, field, value in (
        ("bare", "patch", None),
        ("manual", "applied", None),
        ("manual", "passed", False),
    ):
        row = pair("x", 0, ("first",))
        row[arm][field] = value
        assert not protocol.rescue_targets(row, "manual")


def test_rejected_pretest_makes_no_formal_calls(monkeypatch):
    _, driver, _ = modules(monkeypatch)
    called = []

    def trials(_, phase, ids, repetitions):
        called.append(phase)
        return [pair(t, n) for t in ids for n in range(repetitions)]

    monkeypatch.setattr(driver, "trials", trials)
    result = {"holdout": []}
    driver.run_stages(object(), {"pilot": ["a", "b"]}, result)
    assert called == ["pilot"] and result["phase"] == "pilot_rejected" and result["holdout"] == []


@pytest.mark.parametrize("wins", [False, True])
def test_holdout_is_claimed_only_after_full_release(monkeypatch, tmp_path, wins):
    protocol, driver, _ = modules(monkeypatch)
    from evocoder.contracts import digest

    p = {"pilot": ["p"], "dev": ["d1", "d2"], "holdout": {f: [f + "-0", f + "-1"] for f in protocol.FAMILIES}}

    def trials(_, phase, ids, n):
        if phase == "pilot":
            return [pair(t, i, ("first",)) for i in range(n) for t in ids]
        return [
            {"task": t, "repeat": i, "bare": {"run": t + str(i) + "b"}, "learned": {"run": t + str(i) + "c"}}
            for i in range(n)
            for t in ids
        ]

    monkeypatch.setattr(driver, "trials", trials)

    class Store:
        active = None

        def active_library(self):
            return self.active

        def activate_library(self, library, receipt, **kwargs):
            assert self.active is None and saved["gate"]["passed"] and receipt == digest(saved)
            self.active = library

        def get_artifact(self, k):
            assert k == digest(saved)
            return saved

    store, claimed, saved = Store(), [], {}

    def claim(t, task_hash, study):
        assert store.active == "published" and t not in claimed
        claimed.append(t)

    class Library:
        def __init__(self, _):
            pass

        def provenance(self, _):
            return {"support": {"seed": "hash"}}

        def evaluate(self, skill, dev, support, workers):
            assert [t.id for t in support] == ["seed"]
            saved.update(
                candidate=skill,
                library_id="published",
                gate={"passed": wins},
                rows=[
                    {
                        "task_id": t.id,
                        "repeat": i,
                        "split": "dev",
                        "baseline_run": t.id + str(i) + "b",
                        "candidate_run": t.id + str(i) + "c",
                    }
                    for t in dev
                    for i in range(2)
                ],
            )
            return dict(saved, receipt=digest(saved))

        def compare_pair(self, t, *args, **kwargs):
            assert t.id in claimed
            return {"task_id": t.id, "baseline_pass": False, "candidate_pass": True}

    monkeypatch.setattr(driver, "StudyLibrary", Library)
    names = ["seed", *p["dev"], *(t for ts in p["holdout"].values() for t in ts)]
    s = SimpleNamespace(
        root=tmp_path,
        store=store,
        protocol="study",
        ledger=SimpleNamespace(claim_test=claim),
        tasks={t: SimpleNamespace(id=t, model_dump=lambda n=t: {"id": n}) for t in names},
    )
    result = {"published": False, "holdout": [], "curve": []}
    driver.run_stages(s, p, result)
    assert result["published"] == wins
    assert len(claimed) == len(result["holdout"]) == (6 if wins else 0)
    assert [x["paired_gain"] for x in result["curve"]] == ([1, 1, 1] if wins else [])


def test_authored_transfer_tasks_match_registration_and_withhold_evaluator_code(monkeypatch, tmp_path):
    _, _, cases = modules(monkeypatch)
    from evocoder.contracts import digest

    p = json.loads((Path(__file__).parents[1] / "benchmarks/falsy_transfer/protocol.json").read_text())
    manifest = {r["id"]: r for r in p["cases"]}
    groups = set()
    for name in cases.APPS:
        t, reference = cases.make_case(name, tmp_path)
        assert digest(t.model_dump()) == manifest[t.id]["hash"]
        assert t.source_group not in groups
        groups.add(t.source_group)
        assert set(t.editable) == set(reference)
        assert not set(t.grader_files).intersection(t.files)
        assert all(filename not in t.files["public_test.py"] for filename in t.grader_files)
    assert not set(p["dev"]).intersection(p["pilot"])


def test_object_panel_keeps_previous_losses_with_original_run_ids(monkeypatch):
    modules(monkeypatch)
    from evocoder.contracts import digest
    from run_falsy_objects import CombinedLibrary, LEARNED

    task = SimpleNamespace(id="existing", model_dump=lambda: {"id": "existing"})
    snap = digest(task.model_dump())
    before = {
        "task_id": task.id,
        "task_hash": snap,
        "repeat": 0,
        "baseline_run": "bare",
        "candidate_run": "candidate",
        "baseline_pass": True,
        "candidate_pass": False,
        "baseline_applied_skill": None,
        "candidate_applied_skill": LEARNED,
    }
    lib = object.__new__(CombinedLibrary)
    lib.old = {(task.id, 0): before}
    lib.store = SimpleNamespace(
        run=lambda rid: {
            "task_hash": snap,
            "skill_id": None if rid == "bare" else LEARNED,
            "result": {"passed": rid == "bare"},
        }
    )
    result = lib.compare_pair(task, 0, [], [LEARNED], LEARNED, reverse=False, stop=None)
    assert result == before and result is not before
    lib.old[task.id, 0] = dict(before, candidate_pass=True)
    with pytest.raises(ValueError, match="previous row differs"):
        lib.compare_pair(task, 0, [], [LEARNED], LEARNED, reverse=False, stop=None)


def test_object_panel_does_not_relabel_a_pilot_or_holdout_as_development(monkeypatch):
    modules(monkeypatch)
    from falsy_object_cases import CASES, build
    from evocoder.contracts import digest

    here = Path(__file__).parents[1] / "benchmarks/falsy_transfer"
    q, p = (json.loads((here / name).read_text()) for name in ("object-panel.json", "protocol.json"))
    old = {r["id"] for r in p["cases"]}
    new = [build(name) for name in CASES]
    assert q["holdout"] == p["holdout"] and q["primary"] == digest(p)
    assert len({t.source_group for t, _ in new}) == len(new)
    assert not {t.id for t, _ in new} & old
    cal = {r["task"]: r for r in q["calibration"]}
    for t, repair in new:
        assert (
            digest(t.model_dump()) == cal[t.id]["snapshot"] and digest(repair) == cal[t.id]["reference_patch"]
        )
        assert set(t.grader_files).isdisjoint(t.files)


def test_unreleased_holdout_cannot_be_reported_or_plotted_as_zero_gain(monkeypatch):
    modules(monkeypatch)
    from report_falsy_transfer import verify_holdout

    data = {"holdout": [], "holdout_exposures": 0, "curve": []}
    verify_holdout(data, {}, {}, False, set())
    for update in (
        {"holdout_exposures": 1},
        {"curve": [{"paired_gain": 0.0}]},
        {"holdout": [{"task_id": "unseen"}]},
    ):
        with pytest.raises(ValueError):
            verify_holdout(dict(data, **update), {}, {}, False, set())
