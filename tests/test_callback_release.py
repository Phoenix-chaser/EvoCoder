import sys
from copy import deepcopy
from pathlib import Path

import pytest

from evocoder.contracts import digest
from evocoder.route_shape import public_test_shape

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from callback_policy import block_test, calibrate_noise, comparisons, pilot_summary  # noqa: E402
from callback_protocol import ARMS, PUBLIC, arm_order, code_hashes, validate_activation  # noqa: E402
from callback_sources import sources  # noqa: E402
from portfolio_routing import CHILD  # noqa: E402
from report_click_procedure import read  # noqa: E402
from run_callback_release import design  # noqa: E402


def test_frozen_inputs_and_partial_patch_calibration():
    p = read(PUBLIC)
    assert p["code"] == code_hashes()
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
