"""Check the released holdout's single discordant pair offline, optionally replaying both submissions."""

import argparse
import json
from pathlib import Path

from evocoder.contracts import Settings, digest
from evocoder.optional_benchmark import catalog
from evocoder.runtime import grade, validation, verify_public
from run_falsy_transfer import require

ROOT = Path(__file__).resolve().parents[1]
PROBE = """from service import handle
result = handle({"tenant": {"paginate": False}, "request": {"paginate": None}})
print(result["renderer"]["paginate"])
"""


def replay(execute):
    results = ROOT / "docs/results"
    report = json.loads((results / "workflow-confirmation.json").read_text())
    patches = json.loads((results / "workflow-renderer-pair.json").read_text())
    pairs = [p for p in report["holdout"] if p["baseline_pass"] != p["candidate_pass"]]
    require(len(pairs) == 1 and pairs[0]["task"] == "renderer-02", "holdout discrepancy changed")
    pair = pairs[0]
    require(set(patches) == {pair["baseline_run"], pair["candidate_run"]}, "wrong recorded submissions")
    task = next(t for _, t, _, _ in catalog() if t.id == pair["task"])
    protocol = json.loads((ROOT / "benchmarks/falsy_transfer/workflow-revision.json").read_text())
    settings = Settings(image=protocol["image"])
    indexed = {r["run"]: r for g in report["test_groups"] for r in g["rows"]}
    for rid, patch in patches.items():
        row = indexed[rid]
        require(
            digest(patch) == row["patch"]
            and set(patch) == set(task.editable)
            and digest(task.model_dump()) == row["intent"]["snapshot"],
            "submitted patch or frozen task does not match the recorded run",
        )
        if execute:
            public = verify_public(task, patch, settings, rid)
            independent = grade(task, patch, settings, rid)
            probe = validation(
                {**task.files, **patch, "inspect_layer.py": PROBE},
                ["python", "-B", "inspect_layer.py"],
                settings,
                "renderer-inherit",
            )
            actual = probe["output"].strip()
            require(
                all(r["image_id"] == settings.image and not r["timeout"] for r in (public, independent, probe))
                and public["exit_code"] == probe["exit_code"] == 0
                and independent["passed"] == row["passed"]
                and actual == ("False" if rid == pair["baseline_run"] else "True"),
                "public tests, independent grader or layered override behavior changed",
            )
            print(f"{row['intent']['arm']}  paginate={actual}  independent_pass={independent['passed']}")
    print(f"{len(patches)} submitted patches match their recorded hashes.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true", help="also replay in the registered Docker image")
    replay(parser.parse_args().execute)
