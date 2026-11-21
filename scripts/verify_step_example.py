"""Verify the portable step-seven admission example using only the standard library."""
import argparse
import json
from pathlib import Path

from verify_revision_example import digest


def verify(data):
    skill, receipt = data["original_skill"], data["admission"]
    if (digest(skill) != receipt["candidate"] or skill["steps"][6] != receipt["step"]
            or receipt["model_calls"] != 0 or set(skill["evidence_refs"]) != set(receipt["cited_runs"])):
        raise ValueError("historical version, step or evidence changed")
    observations = {}
    for row in receipt["test_observations"]:
        key = row["run_id"], row["observation_seq"]
        if key in observations or row["run_id"] not in receipt["cited_runs"]:
            raise ValueError("duplicate or uncited test event")
        for name in ("patch_hash", "output_hash"):
            if len(row[name]) != 64 or any(c not in "0123456789abcdef" for c in row[name]):
                raise ValueError("missing output or patch digest")
        observations[key] = row["named_states"]
    bad, good = receipt["claimed"], receipt["positive_control"]
    def statuses(fact):
        return {k: fact[k] for k in ("enabled", "disabled")}
    key = bad["run_id"], bad["observation_seq"]
    actual = observations[key]
    if ((good["run_id"], good["observation_seq"]) != key or statuses(good) != actual
            or any(state == statuses(bad) for state in observations.values())):
        raise ValueError("bad example has a witness or positive control has lost its witness")
    outcomes = [(r["case"], r["accepted"], r["skill_rows_added"]) for r in receipt["checks"]]
    expected = [("archived_unstructured_step", False, 0), ("joint_statuses_without_same_invocation", False, 0),
                ("actual_same_invocation", True, 1)]
    if outcomes != expected:
        raise ValueError("admission result changed")
    return {"cited_runs": len(receipt["cited_runs"]), "historical_public_tests": len(observations),
            "unsupported_combination_witnesses": 0, "observation": key[1], "actual": actual, "checks": outcomes}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("sample", type=Path, nargs="?", default=Path(__file__).resolve().parents[1] / "examples/click-step-seven.json")
    args = parser.parse_args()
    print(json.dumps(verify(json.loads(args.sample.read_text())), ensure_ascii=False, indent=2))
