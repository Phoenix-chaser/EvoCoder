"""Verify the conditional, mechanism-matched panel, including the previous gate's unchanged rows."""

import argparse
from pathlib import Path

from falsy_object_cases import CASES, build
from falsy_transfer_protocol import LEARNED, rescue_targets
from report_click_procedure import read
from report_falsy_transfer import (
    PUBLIC as FIRST_RESULT,
)
from report_falsy_transfer import (
    REGISTRATION as FIRST_PROTOCOL,
)
from report_falsy_transfer import (
    arm_summary,
    audit_archive,
    recalibrate,
    verify_billing,
    verify_holdout,
)
from report_falsy_transfer import (
    verify as verify_primary,
)
from run_falsy_objects import ARMS, sequence
from run_falsy_transfer import require

from evocoder.comparison import dump
from evocoder.contracts import digest
from evocoder.gate import gate_summary, skill_behavior

REGISTRATION = Path("benchmarks/falsy_transfer/object-panel.json")
RESULT = Path("docs/results/falsy-objects.json")


def overview(rows):
    def wins(a):
        return {
            "wins": sum(not r["bare"]["passed"] and r[a]["passed"] for r in rows),
            "losses": sum(r["bare"]["passed"] and not r[a]["passed"] for r in rows),
            "falsy_rescues": sum(bool(rescue_targets(r, a)) for r in rows),
            "overlap_with_manual": sum(
                bool(set(rescue_targets(r, a)) & set(rescue_targets(r, "manual"))) for r in rows
            ),
        }

    matches = []
    for row in rows:
        comparison = "forced_learned" if row["learned"]["applied"] is not None else "bare"
        if row["learned"]["first_solver_request"] == row[comparison]["first_solver_request"]:
            matches.append(
                {
                    "task": row["task"],
                    "repeat": row["repeat"],
                    "comparison": comparison,
                    "grade_agrees": row["learned"]["passed"] == row[comparison]["passed"],
                }
            )
    return {
        "arms": {a: arm_summary(rows, a) for a in ARMS},
        "matched_first_solver_inputs": matches,
        "comparisons": {a: wins(a) for a in ARMS if a != "bare"},
        "routing_skips": [
            {"task": r["task"], "repeat": r["repeat"]} for r in rows if r["learned"]["applied"] is None
        ],
    }


def verify(data, p, first, original):
    verify_primary(first, original)
    require(
        p["primary"] == first["protocol"]
        and p["learned"] == LEARNED
        and p["manual_hash"] == original["manual_hash"]
        and first["phase"] == "development_rejected"
        and not first["published"]
        and first["holdout_exposures"] == 0
        and first["pilot_gate"]["repeated_rescues"].keys()
        & {"transfer-cardinality", "transfer-miss-handler", "transfer-size-hook"}
        and data["primary_receipt"] == first["gate"]["receipt"]
        and data["protocol"] == digest(p),
        "primary study did not authorize this panel",
    )
    manifest = {r["id"]: r for r in original["cases"]}
    cal = {r["task"]: r for r in p["calibration"]}
    require(set(cal) == set(p["dev"]), "missing calibrated object task")
    for name in CASES:
        t, reference = build(name)
        r = cal[t.id]
        require(
            r["snapshot"] == digest(t.model_dump())
            and r["reference_patch"] == digest(reference)
            and not r["broken"]["public"]
            and r["broken"]["controls"]
            and all(r["fixed"].values()),
            "unbound object contract",
        )
        manifest[t.id] = {"id": t.id, "hash": r["snapshot"], "source_group": t.source_group, "split": t.split}
    rows = data["dev"]
    expected = {(t, n) for t in p["dev"] for n in range(p["repeats"])}
    require(
        len(rows) == len(expected) and {(r["task"], r["repeat"]) for r in rows} == expected,
        "incomplete fresh panel",
    )
    seen = set()
    for row in rows:
        t, n = row["task"], row["repeat"]
        require(
            row["snapshot"] == manifest[t]["hash"] and row["order"] == sequence(p["dev"].index(t), n),
            "changed task or arm order",
        )
        for arm in ARMS:
            a = row[arm]
            allowed = {
                "bare": {None},
                "manual": {p["manual_hash"]},
                "learned": {None, LEARNED},
                "forced_learned": {LEARNED},
            }
            require(
                a["run"] not in seen
                and a["steps"] <= 15
                and a["calls"] >= a["steps"]
                and a["applied"] in allowed[arm]
                and a["state"] in ("COMPLETE", "FAILED", "EXHAUSTED")
                and a["passed"] == (a["state"] == "COMPLETE"),
                "invalid object trial",
            )
            seen.add(a["run"])
            require(
                (arm not in ("forced_learned", "manual") or a["route"] == "forced")
                and (arm != "bare" or a["route"] == "none")
                and (arm != "learned" or (a["applied"] is not None) == (a["route"] == "apply")),
                "wrong injection policy",
            )
            if a["patch"]:
                require(
                    set(a["probes"]) == set(cal[t]["fixed"])
                    and all(type(v) is bool for v in a["probes"].values())
                    and all(a["probes"].values()) == a["passed"],
                    "unsupported result for submitted patch",
                )
            else:
                require(a["probes"] is None and not a["passed"], "missing submitted patch")
    gate = data["gate"]
    old_rows = {(r["task_id"], r["repeat"]): r for r in first["gate"]["rows"]}
    require(data["support"] == first["support"], "changed support")
    new = []
    slots = set()
    pairs = {(r["task"], r["repeat"]): r for r in rows}
    for r in gate["rows"]:
        t, n = key = r["task_id"], r["repeat"]
        require(key not in slots, "duplicated release pair")
        slots.add(key)
        if key in old_rows:
            require(r == old_rows[key], "dropped or changed an earlier grade")
            continue
        require(
            key in pairs
            and r["task_hash"] == manifest[t]["hash"]
            and r["source_group"] == manifest[t]["source_group"]
            and r["split"] == "dev"
            and skill_behavior(r, LEARNED, set()) == r["skill_behavior"],
            "unbound new gate row",
        )
        pair = pairs[t, n]
        for label, arm in (("baseline", "bare"), ("candidate", "learned")):
            require(
                (r[label + "_run"], r[label + "_pass"], r[label + "_applied_skill"])
                == (pair[arm]["run"], pair[arm]["passed"], pair[arm]["applied"]),
                "diagnostic arm counted in gate",
            )
        new.append(r)
    require(
        slots == old_rows.keys() | expected
        and gate_summary(new, 2) == data["added_decision"]
        and gate_summary(gate["rows"], 2) == gate["decision"],
        "unsupported release decision",
    )
    released = data["added_decision"]["gate"]["passed"] and gate["decision"]["gate"]["passed"]
    require(
        data["published"] == released
        and data["phase"] == ("complete" if released else "object_gate_rejected"),
        "ineligible release",
    )
    verify_holdout(data, p, manifest, released, seen)
    verify_billing(data, {"dev": ARMS}, seen)
    require(data["summary"] == overview(data["dev"]), "invalid repaired-count summary")
    return {
        "phase": data["phase"],
        "arms": data["summary"]["arms"],
        "paired": data["summary"]["comparisons"],
        "published": data["published"],
        "holdout_pairs": len(data["holdout"]),
        "costs": data["costs"],
    }


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("command", choices=("verify", "export", "calibrate"))
    ap.add_argument("--root", type=Path, default=Path("experiments/falsy-objects-001"))
    ap.add_argument("--registration", type=Path, default=REGISTRATION)
    ap.add_argument("--result", type=Path, default=RESULT)
    ap.add_argument("--first", type=Path, default=FIRST_RESULT)
    ap.add_argument("--first-registration", type=Path, default=FIRST_PROTOCOL)
    ap.add_argument("--image", default="python:3.12-slim")
    args = ap.parse_args()
    p = read(args.registration)
    if args.command == "calibrate":
        recalibrate(p, [build(n) for n in CASES], args.image)
        raise SystemExit(0)
    first, original = read(args.first), read(args.first_registration)
    if args.command == "export":
        data = audit_archive(args.root, p, primary=first)
        data["summary"] = overview(data["dev"])
        result = verify(data, p, first, original)
        dump(args.result, data)
    else:
        result = verify(read(args.result), p, first, original)
    print(result)
