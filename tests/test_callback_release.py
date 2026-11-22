import sys
from copy import deepcopy
from pathlib import Path

import pytest

from evocoder.contracts import digest
from evocoder.route_shape import public_test_shape

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from callback_policy import block_test, calibrate_noise, comparisons, pilot_summary  # noqa: E402
from callback_protocol import ARMS, PUBLIC, arm_order, validate_activation  # noqa: E402
from callback_sources import sources  # noqa: E402
from portfolio_routing import CHILD  # noqa: E402
from report_callback_release import archived_code_hashes  # noqa: E402
from report_click_procedure import read  # noqa: E402
from run_callback_release import design  # noqa: E402


def test_frozen_inputs_and_partial_patch_calibration():
    p = read(PUBLIC)
    assert p["code"] == archived_code_hashes()
    for family, stages in sources().items():
        for stage, (t, patch) in stages.items():
            m = p["families"][family][stage]
            assert (digest(t.model_dump()), digest(patch), digest(public_test_shape(t))) == (
                m["snapshot"],
                m["reference"],
                m["test_shapes"],
            )
            grades = m["calibration"]
            assert not grades["broken"]["public"] and all(grades["reference"].values())
            assert grades["first_file_only"]["controls"] and grades["first_file_only"]["public"]
            assert not all(grades["first_file_only"].values())


def test_observed_aa_threshold_and_sign_flip():
    p = read(PUBLIC)
    hist = read(Path("docs/results/shape-routing.json"))
    n = calibrate_noise(hist, p["config"])
    assert n == p["noise"] and (n["discordant"], n["null_pairs"], n["raw_loss_veto"]) == (7, 20, 6)
    assert n["tail_at_veto"] < n["per_comparison_tail_limit"]
    rows = [dict(repeat=i, parent={"passed": True}, portfolio={"passed": False}) for i in range(7)]
    assert block_test(rows, "parent")["one_sided_signflip_p"] == 1 / 128
    for r in rows:
        r["portfolio"]["passed"] = True
    assert block_test(rows, "parent")["one_sided_signflip_p"] == 1


def test_complete_schedule_and_sealed_entrypoint(tmp_path):
    p = read(PUBLIC)
    assert sum(len(design(p, "gate", n)[0]) * 3 for n in range(7)) == 159
    assert sum(len(design(p, "support", n)[0]) * 3 for n in range(3)) == 72
    assert len({tuple(arm_order("task", n, ARMS)) for n in range(6)}) == 6
    with pytest.raises(ValueError, match="sealed"):
        validate_activation(type("S", (), {"root": tmp_path})())
    with pytest.raises(ValueError):
        design(p, "holdout", 3)


def observations():
    p = read(PUBLIC)
    rows = []
    pilots = []
    for n in range(5):
        for stages in p["families"].values():
            m = stages["pilot"]
            pilots.append(
                dict(
                    repeat=n,
                    task=m["id"],
                    snapshot=m["snapshot"],
                    split="learn",
                    order=["parent"],
                    parent=dict(
                        run=f"{m['id']}-{n}",
                        passed=False,
                        patch="submission",
                        probes=dict(public=True, controls=True, boundary=False),
                    ),
                )
            )
    for phase, count in [("gate", 7), ("support", 3)]:
        for n in range(count):
            ids, arms = design(p, phase, n)
            for t in ids:
                m = p["protected"].get(t) or next(
                    f["confirmation"] for f in p["families"].values() if f["confirmation"]["id"] == t
                )
                won = (
                    t in [p["objects"][0], next(iter(p["families"].values()))["confirmation"]["id"]] and n < 3
                )
                r = dict(task=t, group=m["group"], snapshot=m["snapshot"], split=m["split"], repeat=n)
                for a in arms:
                    child = won and a == "portfolio"
                    r[a] = dict(
                        run=f"{t}-{n}-{a}",
                        state="COMPLETE",
                        image=p["image"],
                        passed=child or not won,
                        route="apply" if child else "none",
                        selected=CHILD if child else None,
                        first_solver_request=digest([t, child]),
                    )
                rows.append(r)
    return p, pilots, rows


def test_gate_requires_new_qualified_source_and_real_input_changes():
    p, pilots, rows = observations()
    freeze = pilot_summary(p, pilots)
    decisions, passed = comparisons(p, freeze, rows)
    assert passed and len(decisions["bare"]["confirmed_object_sources"]) == 2
    for row in pilots:
        row["parent"]["probes"]["controls"] = False
    assert not comparisons(p, pilot_summary(p, pilots), rows)[1]
    first = next(r for r in rows if r["task"] == p["objects"][0])
    first["portfolio"].update(
        selected=None, route="none", first_solver_request=first["parent"]["first_solver_request"]
    )
    assert not comparisons(p, freeze, rows)[1]


def test_config_double_protection_and_incomplete_receipt():
    p, pilots, rows = observations()
    decision = pilot_summary(p, pilots)
    config = [r for r in rows if r["task"] in p["config"]]
    for r in config:
        if r["task"] == p["config"][r["repeat"] % 4]:
            r["portfolio"]["passed"] = False
    d, passed = comparisons(p, decision, rows)
    assert not passed and not d["parent"]["regressions"] and d["parent"]["raw_config_panel"]["veto"]
    first = config[0]
    first["portfolio"].update(first_solver_request=digest("different"), route="apply", selected=CHILD)
    assert first["task"] in comparisons(p, decision, rows)[0]["bare"]["regressions"]
    with pytest.raises(ValueError, match="missing or duplicate"):
        comparisons(p, decision, rows[:-1])
    copy = deepcopy(rows)
    copy[1]["bare"]["run"] = rows[0]["parent"]["run"]
    with pytest.raises(ValueError, match="reused"):
        comparisons(p, decision, copy)


def test_six_raw_config_losses_block_even_when_same_request():
    p, pilots, rows = observations()
    freeze = pilot_summary(p, pilots)
    tid = p["config"][0]
    local = [r for r in rows if r["task"] == tid]
    for r in local[:5]:
        r["portfolio"]["passed"] = False
    d, passed = comparisons(p, freeze, rows)
    assert passed and d["parent"]["tasks"][tid]["same_loss"] == 5
    local[5]["portfolio"]["passed"] = False
    d, passed = comparisons(p, freeze, rows)
    assert not passed and d["parent"]["regressions"] == [tid] and not d["parent"]["raw_config_panel"]["veto"]


def test_pilot_excludes_non_submissions_and_non_green_common_inputs():
    p, pilots, _ = observations()
    family, stage = next(iter(p["families"].items()))
    selected = [r["parent"] for r in pilots if r["task"] == stage["pilot"]["id"]]
    selected[0]["patch"] = None
    selected[1]["probes"]["public"] = False
    selected[2]["probes"]["controls"] = False
    assert pilot_summary(p, pilots)["sources"][family]["qualifying_failure_rounds"] == [4, 5]
    with pytest.raises(ValueError, match="missing or reordered"):
        pilot_summary(p, pilots[1:])


def test_same_request_claim_is_checked_against_reencoded_solver_payload():
    from report_callback_release import wire_audit, workspaces

    p = read(PUBLIC)
    earlier = read(Path("docs/results/shape-routing.json"))
    row = deepcopy(next(x for x in earlier["dev"] if x["task"] == "object-label-policy"))
    trial = {
        "wire_profile": earlier["wire_profile"],
        "pilot_trials": [],
        "dev": [row],
        "support": [],
        "holdout_trials": [],
    }
    tasks = workspaces(p, False)
    assert wire_audit(trial, tasks)["distinct_first_solver_requests"] >= 2
    row["portfolio"]["first_solver_request"] = row["bare"]["first_solver_request"]
    with pytest.raises(ValueError, match="first solver request"):
        wire_audit(trial, tasks)


def test_rejected_receipt_keeps_test_generation_sealed(monkeypatch):
    import report_callback_release as report

    d = read(report.RESULT)
    assert d["gate"]["gate"]["passed"] is False
    d["holdout_trials"] = [d["dev"][0]]

    def forbidden(*args):
        pytest.fail("test corpus was generated before checking the release receipt")

    monkeypatch.setattr(report, "workspaces", forbidden)
    with pytest.raises(ValueError, match="holdout before a passing gate"):
        report.verify(d, read(PUBLIC))


def test_public_accounting_cannot_omit_a_charge():
    import report_callback_release as report

    p = read(PUBLIC)
    d = read(report.RESULT)
    bill = d["charges"][d["pilot_trials"][0]["parent"]["run"]]
    bill["credit_nanos"] += 1
    with pytest.raises(ValueError, match="stage charges"):
        report.schedule_and_billing(d, p, read(report.PREVIOUS_RESULT), report.workspaces(p, False))


def test_ungraded_provider_error_cannot_be_counted_as_a_failed_patch():
    import report_callback_release as report

    d = read(report.RESULT)
    missing = d["gate"]["ungraded_support"][0]
    row = next(r for r in d["support"] if r["task"] == missing["task"] and r["repeat"] == missing["repeat"])
    arm = row[missing["arm"]]
    assert arm["passed"] is None and arm["patch"] is None
    arm["passed"] = False
    with pytest.raises(ValueError):
        report.verify(d, read(PUBLIC))
