"""Build the predeclared Click pilot from a local upstream Git mirror."""

import argparse
import json
from pathlib import Path

from evocoder.contracts import canonical, digest
from mine_benchmark import build


def prepare(repo: Path, output: Path) -> None:
    if output.exists():
        raise ValueError("pilot bundle destination must be new")
    catalog_path = Path(__file__).resolve().parents[1] / "benchmarks/comparison/click-pilot.json"
    catalog = json.loads(catalog_path.read_text())
    cases = []
    used_locations = set()
    for entry in catalog["tasks"]:
        task, control = build(repo, catalog["repository"], entry["commit"])
        locations = set(control["functions"])
        if locations & used_locations:
            raise ValueError("pilot includes overlapping changed functions")
        used_locations.update(locations)
        task = task.model_copy(update={"split": entry["split"], "issue": entry["issue"]})
        body = task.model_dump()
        control["task_hash"] = digest(body)
        control["split"] = task.split
        cases.append((task, body, control))
    (output / "tasks").mkdir(parents=True)
    (output / "controller").mkdir()
    entries = []
    for task, body, control in cases:
        path = f"tasks/{task.id}.json"
        (output / path).write_text(canonical(body) + "\n")
        (output / "controller" / f"{task.id}.json").write_text(canonical(control) + "\n")
        entries.append({
            "path": path, "hash": digest(body), "id": task.id, "split": task.split,
            "source_group": task.source_group, "repository": task.repository,
        })
    manifest = {
        "schema": 1,
        "family": catalog["family"],
        "catalog_hash": digest(catalog),
        "seed_task_id": catalog["seed_task_id"],
        "tasks": entries,
    }
    (output / "manifest.json").write_text(canonical(manifest) + "\n")
    print(f"{len(entries)} tasks frozen, manifest {digest(manifest)}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    prepare(args.repo, args.output)
