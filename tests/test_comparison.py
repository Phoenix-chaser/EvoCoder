import pytest

from evocoder.statistics import summarize
from evocoder.store import Store, nanos


def test_shared_ledger_reservations_cannot_reset_costs_or_detach(settings, store, tmp_path):
    one = Store(tmp_path / "one", ledger=store)
    two = Store(tmp_path / "two", ledger=store)
    call = one.reserve("r", 100, 60)
    with pytest.raises(ValueError, match="exhausted"):
        two.reserve("s", 100, 60)
    one.settle(call, 40, {})
    assert two.budget()["spent_usd"] == 40e-9
    with pytest.raises(ValueError, match="original shared ledger"):
        Store(tmp_path / "one")
    with pytest.raises(ValueError, match="original shared ledger"):
        Store(tmp_path / "one", ledger=Store(tmp_path / "different"))
    assert store.costs(["r"])["calls"] == 1


def test_authorized_grant_is_idempotent_and_preserves_usage(store):
    call = store.reserve("r", nanos(1), 100)
    with pytest.raises(ValueError, match="unsettled"):
        store.grant_budget("user-10", nanos(10), nanos(1), "Synthetic budget-grant fixture")
    store.settle(call, 80, {})
    before = store.costs(["r"])
    for _ in range(2):
        store.grant_budget("user-10", nanos(10), nanos(1), "Synthetic budget-grant fixture")
    assert store.budget()["cap_usd"] == 11 and store.costs(["r"]) == before
    with pytest.raises(ValueError, match="different authorization"):
        store.grant_budget("user-10", nanos(20), nanos(1), "Synthetic budget-grant fixture")


def results(count=3, repeats=3):
    return [
        {
            "task_id": f"t{i}",
            "source_group": f"g{i}",
            "repeat": r,
            "arm": a,
            "passed": a == "D",
            "cost_usd": 0.01,
            "state": "COMPLETE",
        }
        for i in range(count)
        for r in range(repeats)
        for a in "ABCD"
    ]


def test_statistics_average_repeats_and_do_not_claim_independent_tasks():
    report = summarize(results(), ["t0", "t1", "t2"], 3, bootstrap_samples=100)
    assert report["complete"] and report["paired_tasks"] == 3
    assert report["contrasts"]["D_vs_C"]["clusters"] == 3
    assert report["contrasts"]["D_vs_C"]["ci95"] == [1, 1]
    assert not report["supports_positive_primary_effect"]  # Three tasks remain a small pilot.
    with pytest.raises(ValueError, match="duplicate"):
        summarize([*results(), results()[0]], ["t0", "t1", "t2"], 3)


def test_incomplete_grid_never_supports_a_positive_claim():
    report = summarize(results()[:-1], ["t0", "t1", "t2"], 3, bootstrap_samples=100)
    assert not report["complete"] and not report["supports_positive_primary_effect"]
    assert report["paired_tasks"] == 2


def test_fully_recorded_grid_with_infrastructure_error_cannot_support_gain():
    rows = results(count=20, repeats=1)
    next(row for row in rows if row["arm"] == "C")["state"] = "ERROR"
    report = summarize(rows, [f"t{i}" for i in range(20)], 1)
    assert report["complete"] and not report["valid_complete"]
    assert report["contrasts"]["D_vs_C"]["ci95"] == [1, 1]
    assert not report["supports_positive_primary_effect"]


def test_source_group_is_bootstrap_unit_not_number_of_repeated_solutions():
    rows = results()
    for row in rows:
        row["source_group"] = "one_related_group"
    report = summarize(rows, ["t0", "t1", "t2"], 3)
    assert report["contrasts"]["D_vs_C"]["clusters"] == 1
    assert report["contrasts"]["D_vs_C"]["ci95"] is None
    assert not report["supports_positive_primary_effect"]


@pytest.mark.parametrize("reject_first_revision", [False, True])
@pytest.mark.parametrize(
    "interruption,use_credits,pilot",
    [(stop, credits, False) for stop in (None, ("D", 402, 0), ("C", 500, 11)) for credits in (False, True)]
    + [(None, True, True)],
)
def test_four_arm_orchestration_freezes_shared_seeds_and_never_learns_from_test(
    tmp_path, monkeypatch, reject_first_revision, interruption, use_credits, pilot
):
    import difflib
    import importlib
    import json
    from evocoder.comparison import execute, prepare, prepare_continuation, state_snapshot
    from evocoder.contracts import MODEL_TASK_VIEW, Settings, canonical, digest
    from evocoder.dataset import fixture_task
    from evocoder.model import Model
    from evocoder.store import uid

    comparison = importlib.import_module("evocoder.comparison")
    stream = importlib.import_module("evocoder.library_stream")
    unit = "credits" if use_credits else "usd"
    ledger = Store(tmp_path / "ledger", unit=unit)
    call = ledger.reserve("previous", nanos(11), 1)
    ledger.settle(call, 0, {})
    tasks = []
    entries = []
    repositories = ["tkem/cachetools", "more-itertools/more-itertools", "pallets/click"]
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    split_sizes = {"learn": 3, "dev": 4, "test": 3} if pilot else {"learn": 32, "dev": 16, "test": 32}
    for split, count in split_sizes.items():
        for i in range(count):
            task = fixture_task(f"{split}-{i}", split, False).model_copy(
                update={"repository": repositories[0 if pilot else i % 3]}
            )
            (bundle / f"{task.id}.json").write_text(canonical(task.model_dump()))
            entries.append({"path": f"{task.id}.json", "hash": digest(task.model_dump())})
            tasks.append(task)
    manifest = {"schema": 1, "tasks": entries}
    (bundle / "manifest.json").write_text(canonical(manifest))
    (bundle / "verification.json").write_text(
        canonical(
            {
                "passed": True,
                "manifest_hash": digest(manifest),
                "image_id": "sha256:controlled-fixture",
                "rows": [{"id": t.id, "task_hash": digest(t.model_dump()), "passed": True} for t in tasks],
            }
        )
    )
    settings = Settings(
        _env_file=None,
        mode="live",
        state_dir=ledger.root,
        base_url="https://open.bigmodel.cn/api/coding/paas/v4" if use_credits else "https://unit.invalid/v1",
        model="controlled-fixture",
        api_key="secret",
        budget_usd=11,
        input_price_per_million=0.15,
        output_price_per_million=0.6,
        provider="glm" if use_credits else "compatible",
        billing_mode="subscription_credits" if use_credits else "token_usd",
        credit_budget=11,
        input_credits_per_million=690,
        cached_input_credits_per_million=170,
        output_credits_per_million=2400,
    )
    proposal_calls = []
    solve_attempts = []
    deploy_revision = not (reject_first_revision and interruption is None)
    revision_attempt = 2 if pilot else 4
    revision_devs = {f"dev-{index}" for index in range(split_sizes["dev"] - 2, split_sizes["dev"])}

    def scripted_proposal(self, schema, prompt, data, run_id):
        assert schema.__name__ == "Skill"
        assert all("id" not in e["task"] and "source_group" not in e["task"] for e in data["evidence"]["episodes"])
        proposal_calls.append(data)
        if reject_first_revision and len(proposal_calls) == revision_attempt:
            from evocoder.errors import ProposalRejected

            raise ProposalRejected("controlled settled invalid proposal")
        skill = Model.mock_skill(data)
        changes = {"steps": [*skill.steps, f"Verify controlled variation {len(proposal_calls)}"]}
        if data["parent"]:
            from evocoder.evolution import RECEIPT_MARKER

            assert set(data["verified_learning_observations"]) == set(data["allowed_evidence_refs"])
            changes["change_summary"] = RECEIPT_MARKER
        return skill.model_copy(update=changes)

    monkeypatch.setattr(Model, "request", scripted_proposal)

    class ControlledRunner:
        def __init__(self, settings, store, *args):
            self.settings, self.store = settings, store

        def solve(
            self,
            task,
            skill_id=None,
            *,
            use_active=False,
            purpose="solve",
            skill_ids=None,
            library_id=None,
            experience_pool_id=None,
            run_id=None,
            **kwargs,
        ):
            assert not use_active
            if library_id:
                skill_ids = self.store.library_members(library_id, mode="live", require_published=True)
            chosen = skill_ids[0] if skill_ids else None
            if purpose == "solve" and task.split == "learn" and self.store.active_library():
                active = self.store.library_members(self.store.active_library(), mode="live")
                chosen = next((key for key in (skill_ids or []) if key in active), chosen)
            passed = bool(chosen) if purpose == "evaluate" else False
            if deploy_revision and purpose == "evaluate" and task.id in revision_devs:
                chosen = next((key for key in (skill_ids or []) if self.store.get_artifact(key)["parent"]), None)
                passed = chosen is not None
            key = run_id or uid()
            solve_attempts.append((self.store.root.parent.name, self.store.root.name, task.id, purpose))
            interrupted = (
                interruption
                and self.store.root.parent.name == "experiment"
                and self.store.root.name == interruption[0]
                and task.id == f"learn-{interruption[2]}"
                and purpose == "solve"
            )
            if interrupted:
                self.store.reserve(key, nanos(11), 100)
            submitted = not interrupted and (purpose == "evaluate" or chosen is not None)
            artifact = None
            if submitted:
                source = task.editable[0]
                before = task.files[source]
                expression = '"" if value is None else value.strip().lower()' if passed else "value.lower()"
                after = before.replace("value.strip().lower()", expression)
                assert before != after
                artifact = self.store.artifact("patch", {source: after})
                diff = "".join(difflib.unified_diff(before.splitlines(True), after.splitlines(True),
                                                   fromfile="a/" + source, tofile="b/" + source))
            result = {
                "passed": passed,
                "public_failed": not passed and not interrupted,
                "patch": artifact,
                "image_id": "sha256:controlled-fixture",
                "routing": {"decision": "apply" if chosen else "none"},
                "model_steps": 2,
                "cost": self.store.costs([key]),
                "error": f"RuntimeError: model_http_{interruption[1]}: reservation retained" if interrupted else None,
            }
            with self.store.connect(write=True) as db:
                db.execute(
                    "INSERT INTO runs VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        key,
                        task.id,
                        task.split,
                        digest(task.model_dump()),
                        chosen,
                        "live",
                        purpose,
                        "ERROR"
                        if interrupted
                        else ("COMPLETE" if passed else "FAILED")
                        if submitted
                        else "EXHAUSTED",
                        0,
                        canonical(result),
                        "{}",
                    ),
                )
            self.store.event(key, "START", {"task": task.public(), "model_task_view": MODEL_TASK_VIEW})
            public = {"action": {"tool": "test", "argv": task.public_command},
                      "observation": {"exit_code": int(not passed), "timeout": False,
                                      "image_id": result["image_id"]}}
            if submitted:
                self.store.event(key, "OBSERVATION", {"action": {"tool": "edit", "path": source},
                                                     "observation": {"exit_code": 0}})
                self.store.event(key, "PATCH", {"artifact": artifact, "diff": diff})
                self.store.event(key, "PUBLIC_VERIFY", public)
                self.store.event(key, "GRADE", {
                    "passed": passed, "timeout": False, "image_id": result["image_id"],
                    "grader_hash": digest({"files": task.grader_files, "command": task.grader_command}),
                })
            elif not interrupted:
                self.store.event(key, "OBSERVATION", public)
            if task.split == "test":
                assert purpose == "evaluate"
            return key

    monkeypatch.setattr(comparison, "Runner", ControlledRunner)
    monkeypatch.setattr(stream, "Runner", ControlledRunner)
    root = tmp_path / "experiment"
    prepare(
        settings, bundle / "manifest.json", bundle / "verification.json", root, repeats=1, workers=2,
        pilot_seed="learn-0" if pilot else None,
    )
    result = execute(settings, root)
    if interruption:
        arm, code, failed_index = interruption
        assert not result["completed"] and result["phase"] == f"learn_{arm}"
        old_control = state_snapshot(Store(root / "C", ledger=ledger))
        old_timeline = json.loads((root / f"learning-{arm}/timeline.json").read_text())
        assert len(old_timeline) == failed_index + 1 and old_timeline[-1]["state"] == "ERROR"
        from evocoder.library_stream import learning_checkpoint

        broken = tmp_path / "changed-checkpoint"
        broken.mkdir()
        (broken / "timeline.json").write_text(canonical(old_timeline))
        saved = json.loads((root / f"learning-{arm}/summary.json").read_text())
        (broken / "summary.json").write_text(canonical({**saved, "proposal_attempts": 1}))
        with pytest.raises(ValueError, match="checkpoint"):
            learning_checkpoint(Store(root / arm, ledger=ledger), bundle / "manifest.json", broken, "live")
        if arm == "C":
            assert not (root / "D").exists()  # auditing must not create an absent state
        original = root
        if arm == "C" and use_credits and not reject_first_revision:
            with pytest.raises(ValueError, match="unsettled calls without evidence"):
                prepare(settings, bundle / "manifest.json", bundle / "verification.json", tmp_path / "unaudited")
            prepared_with_reservations = prepare(
                settings, bundle / "manifest.json", bundle / "verification.json", tmp_path / "audited",
                carry_reservations_from=[original],
            )
            carried = prepared_with_reservations["historical_reservations"]["calls"]
            assert len(carried) == 1 and carried[0]["run_id"] == old_timeline[-1]["run_id"]
            assert ledger.budget() == result["budget_after"]
        root = tmp_path / "continued"
        prepared = prepare_continuation(settings, original, root)
        assert len(prepared["continuation"]["preserved_reservations"]) == 1
        assert prepared["continuation"]["resume_arm"] == arm
        assert prepared["continuation"]["retained_tasks"] == len(old_timeline)
        assert ledger.budget()["reserved_" + unit] == 100e-9
        # A changed frozen parent cannot be continued under the already prepared protocol.
        changed = Store(original / arm, ledger=ledger)
        changed.event(old_timeline[-1]["run_id"], "EXTERNAL_AUDIT", {"test": "changed checkpoint"})
        from evocoder.comparison import continuation_snapshot

        assert continuation_snapshot(settings, original, ledger) != prepared["continuation"]
        with changed.connect(write=True) as db:
            db.execute("DELETE FROM events WHERE kind='EXTERNAL_AUDIT'")  # test fixture only
        with monkeypatch.context() as patch:
            original_sources = comparison.sources
            patch.setattr(comparison, "sources", lambda: {**original_sources(), "new_solver.py": "changed"})
            with pytest.raises(ValueError, match="solver or evaluation policy"):
                continuation_snapshot(settings, original, ledger)
        with pytest.raises(ValueError, match="unchanged pre-test"):
            continuation_snapshot(settings.model_copy(update={"max_steps": 99}), original, ledger)
        result = execute(settings, root)
        assert state_snapshot(Store(root / "C", ledger=ledger)) != old_control  # new test runs only
        assert state_snapshot(Store(original / "C", ledger=ledger)) == old_control
        continued = json.loads((root / f"learning-{arm}/timeline.json").read_text())
        assert len(continued) == 32 and continued[:len(old_timeline)] == old_timeline
        assert len({r["run_id"] for r in continued}) == 32
        assert not any(
            parent == "continued" and folder == arm and task == f"learn-{failed_index}" and purpose == "solve"
            for parent, folder, task, purpose in solve_attempts
        )
        assert ledger.budget()["reserved_" + unit] == 100e-9  # never fabricate a zero settlement
        assert sum(p.get("reused", False) for p in result["phases"]) == (3 if arm == "D" else 2)
    assert result["completed"] == deploy_revision, result.get("error")
    assert result["test_preflight"]["passed"] == deploy_revision
    assert bool(result["test_preflight"]["revision_to_frozen_version"]) == deploy_revision
    assert result["statistics"]["observed_cells"] == (4 * split_sizes["test"] if deploy_revision else 0)
    if deploy_revision and interruption is None:
        empty_control = Store(tmp_path / "empty-control", ledger=ledger)
        published_revision = Store(root / "D", ledger=ledger)
        preflight = comparison.revision_test_preflight({"C": empty_control, "D": published_revision}, settings)
        assert published_revision.active_library()
        assert not preflight["passed"]
    if not deploy_revision:
        assert result["phase"] == "no_revised_library"
        with ledger.connect() as db:
            assert db.execute("SELECT COUNT(*) FROM test_exposures").fetchone()[0] == 0
    assert len(proposal_calls) == (2 if pilot else 6)  # Shared roots plus D revisions only.
    assert result["statistics"]["contrasts"]["D_vs_C"]["paired_cluster_macro_difference"] == (
        0 if deploy_revision else None
    )
    assert not result["statistics"]["supports_positive_primary_effect"]
    assert json.loads((root / "learning-C/summary.json").read_text())["proposal_count"] == 0
    final_d = json.loads((root / "learning-D/summary.json").read_text())
    assert final_d["proposal_count"] == (1 if pilot else 3) - int(reject_first_revision)
    assert final_d["proposal_attempts"] == (1 if pilot else 3)
    if pilot:
        assert [row["task_id"] for row in final_d["timeline"]] == ["learn-1", "learn-2"]
    with pytest.raises(FileExistsError):
        execute(settings, root)


def test_bounded_dispatch_stops_instead_of_queuing_future_paid_trials():
    from evocoder.jobs import bounded_map

    attempted = []

    def job(index):
        attempted.append(index)
        if index == 0:
            raise RuntimeError("uncertain response")
        return index

    with pytest.raises(RuntimeError, match="uncertain response"):
        bounded_map(job, list(range(100)), 2)
    assert set(attempted) <= set(range(2))


def test_mismatched_pair_group_cannot_distort_confidence_interval():
    rows = results()
    rows[0]["source_group"] = "different"
    with pytest.raises(ValueError, match="source group changed"):
        summarize(rows, ["t0", "t1", "t2"], 3)


def test_price_allowance_uses_capacity_without_fabricating_provider_calls(store):
    call = store.reserve("r", 100, 60)
    store.settle(call, 40, {})
    for _ in range(2):
        store.hold_budget("earlier-price", 20, "Conservative allowance for earlier pricing")
    assert store.budget()["held_usd"] == 20e-9
    assert store.costs(["r"])["calls"] == 1
    with pytest.raises(ValueError, match="exhausted"):
        store.reserve("s", 100, 41)


def test_shared_test_exposure_prevents_rebranding_seen_tasks_as_unseen(store, tmp_path):
    arm = Store(tmp_path / "arm", ledger=store)
    store.claim_test("task", "hash", "experiment-one")
    arm.claim_test("task", "hash", "experiment-one")
    with pytest.raises(ValueError, match="different experiment"):
        arm.claim_test("task", "hash", "experiment-two")


def test_reused_seed_snapshot_cannot_include_later_revisions(settings, store):
    from evocoder.comparison import seed_snapshot
    from evocoder.dataset import fixture_task
    from test_library import seed

    first = fixture_task("seed", "learn", False)
    parent = seed(settings, store, first)
    assert seed_snapshot(store, settings, ["seed"])["root_ids"] == [parent]
    seed(settings, store, fixture_task("later", "learn", True), parent)
    with pytest.raises(ValueError, match="unmodified roots"):
        seed_snapshot(store, settings, ["seed", "later"])
