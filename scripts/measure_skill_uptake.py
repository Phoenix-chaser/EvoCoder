"""Compare observed solver actions in existing paired GLM trials. No model or sandbox calls."""
import argparse
from collections import Counter, defaultdict
from fractions import Fraction
from pathlib import Path
from statistics import mean

from evocoder.comparison import dump
from evocoder.contracts import digest
from report_click_procedure import Archive, read


def action_distance(left, right):
    """Total variation of within-attempt tool proportions. Empty traces are not distributions."""
    if not sum(left.values()) or not sum(right.values()):
        raise ValueError("paired trials require at least one solver action in each arm")
    return sum(abs(Fraction(left.get(k, 0), sum(left.values())) - Fraction(right.get(k, 0), sum(right.values())))
               for k in left.keys() | right.keys()) / 2


def paired_actions(root, *, verify_checks=False):
    protocol, result = read(root / "protocol.json"), read(root / "result.json")
    if not result["completed"] or result["protocol"] not in (protocol, digest(protocol)):
        raise ValueError("incomplete or unbound experiment")
    store = Archive(root / "seed/evocoder.sqlite3")
    pairs, seen = [], set()
    for row in result["rows"]:
        if row["split"] != "dev" or row["infrastructure_error"]:
            raise ValueError("expected independently scored development pairs")
        pair = {k: row[k] for k in ("task_id", "repeat", "policy")}
        for arm, version in (("baseline", "parent"), ("candidate", "child")):
            key = row[arm + "_run"]
            run = store.run(key)
            if (key in seen or run["mode"] != "live" or run["task_hash"] != row["task_hash"]
                    or run["task_id"] != row["task_id"] or run["result"]["passed"] != row[arm + "_pass"]
                    or run["skill_id"] != row[arm + "_applied_skill"] or run["state"] in ("ERROR", "INTERRUPTED")):
                raise ValueError("run provenance differs from pair receipt")
            seen.add(key)
            events = store.events(key)
            route, = [e["payload"] for e in events if e["kind"] == "SKILL_ROUTE"]
            if (route["decision"] != row[arm + "_route"] or route["applied_skill_id"] != run["skill_id"]
                    or (run["skill_id"] is not None and run["skill_id"] != protocol[version])):
                raise ValueError("skill exposure differs from the execution trace")
            if row["policy"] == "fixed_skip":
                first = next(e["payload"] for e in events if e["kind"] == "MODEL_RESPONSE"
                             and e["payload"].get("expected_function") == "submit_action")
                if (first["request_body_hash"] != row[arm + "_first_solver_request_hash"]
                        or first["request_body_hash"] != row["baseline_first_solver_request_hash"]):
                    raise ValueError("negative control did not start with identical solver input")
            actions = [e["payload"]["tool"] for e in events if e["kind"] == "ACTION"]
            data = {"run": key, "route": route["decision"], "applied": route["applied"], "passed": row[arm + "_pass"],
                    "counts": dict(sorted(Counter(actions).items())), "steps": len(actions),
                    "rejected": sum(e["kind"] == "ACTION_REJECTED" for e in events)}
            if verify_checks:
                trial = read(root / "assertions" / (key + ".json"))
                if digest(trial) != row[arm + "_behavior_hash"] or trial["run"] != key:
                    raise ValueError("unbound authored-check receipt")
                data["detected_on_submission"] = trial["detected_on_submission"]
            pair[arm] = data
        pair["tv"] = float(action_distance(pair["baseline"]["counts"], pair["candidate"]["counts"]))
        pairs.append(pair)
    return {"protocol": digest(protocol), "pairs": pairs}


def summarize(pairs):
    return {"pairs": len(pairs), "mean_tv": mean(p["tv"] for p in pairs),
            "both_applied": sum(p["baseline"]["applied"] and p["candidate"]["applied"] for p in pairs),
            **{arm + "_repairs": sum(p[arm]["passed"] for p in pairs) for arm in ("baseline", "candidate")}}


def measure(matrix_root, switch_root):
    matrix = paired_actions(matrix_root)
    latest = paired_actions(switch_root, verify_checks=True)
    grouped = defaultdict(dict)
    for p in matrix["pairs"]:
        key = (p["task_id"], p["repeat"])
        if key in grouped[p["policy"]]:
            raise ValueError("repeated task/round identifier")
        grouped[p["policy"]][key] = p
    apply, skip = grouped["fixed_apply"], grouped["fixed_skip"]
    if not apply or apply.keys() != skip.keys() or apply.keys() != grouped["normal"].keys():
        raise ValueError("unmatched policy controls")
    comparisons = []
    for key, pair in sorted(apply.items()):
        control = skip[key]
        if (not all(pair[a]["applied"] for a in ("baseline", "candidate"))
                or any(control[a]["applied"] for a in ("baseline", "candidate"))):
            raise ValueError("wrong exposure in fixed policy")
        def distance(p):
            return action_distance(p["baseline"]["counts"], p["candidate"]["counts"])
        delta = distance(pair) - distance(control)
        comparisons.append({"task": key[0], "repeat": key[1], "excess_tv": float(delta), "above_control": delta > 0})
    return {"definition": "TV on solver ACTION tool names, normalized within run; strict excess over matched fixed-skip pair",
            "matrix": matrix, "matrix_by_policy": {k: summarize(list(v.values())) for k, v in grouped.items()},
            "matched_controls": comparisons, "uptake_numerator": sum(c["above_control"] for c in comparisons),
            "uptake_denominator": len(comparisons), "mean_excess_tv": mean(c["excess_tv"] for c in comparisons),
            "switch": latest, "switch_summary": {**summarize(latest["pairs"]), **{
                arm + "_detections": sum(p[arm]["detected_on_submission"] for p in latest["pairs"])
                for arm in ("baseline", "candidate")}}, "new_model_calls": 0}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--matrix", type=Path, default=Path("experiments/glm-click-routing-matrix-002"))
    parser.add_argument("--switch", type=Path, default=Path("experiments/glm-click-switch-feedback-001"))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    dump(args.output, measure(args.matrix, args.switch))
