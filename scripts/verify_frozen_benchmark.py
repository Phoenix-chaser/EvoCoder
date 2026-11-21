"""Bind final split/task hashes to fresh original/reference execution receipts."""

import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from evocoder.contracts import Settings, canonical, digest
from evocoder.dataset import load_catalog
from evocoder.sandbox import bounded_process
from verify_candidates import check

ROOT = Path("experiments/comparison-benchmark-001")


def main():
    target = ROOT / "verification.json"
    if target.exists():
        raise ValueError("verification already exists")
    image = bounded_process(
        ["docker", "image", "inspect", "evocoder-comparison:py312-v1", "--format", "{{.Id}}"], 10
    )["output"].strip()
    options = Settings(_env_file=None, mode="mock", image=image)

    manifest = json.loads((ROOT / "manifest.json").read_text())

    def verify(task):
        control = json.loads((ROOT / "controller" / f"{task.id}.json").read_text())
        row = {
            "id": task.id,
            "task_hash": digest(task.model_dump()),
            "reference_hash": digest(control["reference"]),
            "passed": False,
            "image_id": image,
            "checks": {},
        }
        try:
            if row["task_hash"] != control["task_hash"]:
                raise ValueError("controller/task mismatch")
            for name, files, cmd in [
                ("buggy_regression", {**task.files, **task.grader_files}, task.grader_command),
                (
                    "reference_regression",
                    {**task.files, **task.grader_files, **control["reference"]},
                    task.grader_command,
                ),
                ("reference_public", {**task.files, **control["reference"]}, task.public_command),
                ("buggy_public", task.files, task.public_command),
            ]:
                result = check(files, cmd, options, task.id + "-frozen-" + name)
                row["checks"][name] = result
                j = result["junit"]
                if not j or j["errors"] or j["tests"] <= j["skipped"]:
                    raise ValueError("invalid collected tests")
                if name == "buggy_regression" and not (result["exit_code"] == 1 and j["failures"]):
                    raise ValueError("not reproduced")
                if name.startswith("reference") and result["exit_code"] != 0:
                    raise ValueError("reference fails")
                if name == "buggy_public" and result["exit_code"] not in {0, 1}:
                    raise ValueError("public environment fails")
                if (
                    name == "buggy_public"
                    and manifest.get("requires_public_reproduction")
                    and task.id != manifest["seed_task_id"]
                    and not (result["exit_code"] == 1 and j["failures"])
                ):
                    raise ValueError("public reproduction did not fail on original source")
            row["passed"] = True
        except Exception as exc:
            row["error"] = str(exc)[:700]
        return row

    with ThreadPoolExecutor(max_workers=4) as pool:
        rows = []
        for future in as_completed([pool.submit(verify, t) for t in load_catalog(ROOT / "manifest.json")]):
            row = future.result()
            rows.append(row)
            print({"done": len(rows), "passed": sum(r["passed"] for r in rows), "id": row["id"]}, flush=True)
    receipt = {
        "schema": 1,
        "manifest_hash": digest(manifest),
        "image_id": image,
        "passed": all(r["passed"] for r in rows),
        "rows": rows,
        "model_calls": 0,
    }
    target.write_text(canonical(receipt) + "\n")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=ROOT)
    ROOT = parser.parse_args().root
    main()
