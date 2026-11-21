"""Choose 80 model-blind eligible defects, function-disjoint and chronological within repository."""

import json
import shutil
from collections import defaultdict
from pathlib import Path

from evocoder.contracts import Task, canonical, digest
from evocoder.dataset import load_catalog
from evocoder.sandbox import bounded_process

CANDIDATES = Path("work/benchmark-candidates")
PROOFS = Path("work/benchmark-verification")
OUT = Path("experiments/comparison-benchmark-001")
QUOTAS = {
    "dateutil/dateutil": (1, 1, 1),
    "mahmoud/boltons": (11, 5, 10),
    "more-itertools/more-itertools": (4, 2, 4),
    "pallets/click": (9, 4, 9),
    "python-attrs/attrs": (3, 2, 3),
    "theskumar/python-dotenv": (2, 1, 2),
    "tkem/cachetools": (2, 1, 3),
}


def main():
    if OUT.exists():
        raise ValueError("frozen destination exists")
    eligible = []
    for path in sorted(PROOFS.glob("*.json")):
        proof = json.loads(path.read_text())
        if proof.get("passed") is not True:
            continue
        folder = CANDIDATES / proof["id"]
        controller = json.loads((folder / "controller.json").read_text())
        eligible.append((proof, controller, folder))
    seen = set()
    by_repo = defaultdict(list)
    dropped = []
    for proof, controller, folder in eligible:
        keys = {controller["repository"] + "::" + f for f in controller["functions"]}
        if keys & seen:
            dropped.append({"id": proof["id"], "reason": "overlapping changed-function group"})
            continue
        seen.update(keys)
        repo = Path("work/benchmark-repos") / controller["repository"].replace("/", "--")
        timestamp = int(
            bounded_process(
                ["git", "-C", str(repo), "show", "-s", "--format=%ct", controller["fixed_commit"]], 10
            )["output"]
        )
        controller["timestamp"] = timestamp
        by_repo[controller["repository"]].append((proof, controller, folder))
    selected = []
    for repository, quotas in QUOTAS.items():
        rows = by_repo[repository]
        total = sum(quotas)
        if len(rows) < total:
            raise ValueError(f"{repository} has {len(rows)} eligible, needs {total}")
        # Selection does not use model outcomes. Excess cases are ordered by a fixed hash salt.
        rows = sorted(rows, key=lambda r: digest(["comparison-selection-20261117", r[0]["id"]]))[:total]
        rows.sort(key=lambda r: (r[1]["timestamp"], r[0]["id"]))
        cursor = 0
        for split, count in zip(("learn", "dev", "test"), quotas, strict=True):
            for proof, controller, folder in rows[cursor : cursor + count]:
                selected.append((split, proof, controller, folder))
            cursor += count
    OUT.mkdir(parents=True)
    for name in ("tasks", "controller", "eligibility"):
        (OUT / name).mkdir()
    entries = []
    cases = []
    for split, proof, controller, folder in selected:
        task = Task.model_validate_json((folder / "task.json").read_text()).model_copy(
            update={"split": split}
        )
        assert (
            digest(Task.model_validate_json((folder / "task.json").read_text()).model_dump())
            == controller["task_hash"]
        )
        controller = {
            **controller,
            "pre_split_task_hash": controller["task_hash"],
            "task_hash": digest(task.model_dump()),
            "split": split,
        }
        (OUT / "tasks" / f"{task.id}.json").write_text(canonical(task.model_dump()) + "\n")
        (OUT / "controller" / f"{task.id}.json").write_text(canonical(controller) + "\n")
        shutil.copy2(PROOFS / f"{task.id}.json", OUT / "eligibility" / f"{task.id}.json")
        entries.append(
            {
                "id": task.id,
                "path": f"tasks/{task.id}.json",
                "hash": digest(task.model_dump()),
                "split": split,
                "repository": task.repository,
                "source_group": task.source_group,
                "timestamp": controller["timestamp"],
                "functions": controller["functions"],
                "family": task.repository.split("/")[1],
            }
        )
        catalog_row = {k: controller[k] for k in (
            "repository", "base_commit", "fixed_commit", "message", "task_hash", "split", "functions",
            "patch_fingerprint", "timestamp",
        )}
        # The pinned commit reconstructs the full task. Its subject suffices for the shareable index.
        catalog_row["message"] = controller["message"].splitlines()[0]
        cases.append(catalog_row)
    manifest = {
        "schema": 1,
        "kind": "historical_commit_defects",
        "claim": "model-blind selected historical defect reconstructions; not official SWE-bench or contamination-free",
        "selection": "eligible original assertion failures; one case per nonoverlapping changed function; max 26 per repository; chronological 32/16/32 split",
        "repository_disjoint": False,
        "function_disjoint": True,
        "cross_repository_generalization_test": False,
        "tasks": entries,
    }
    (OUT / "manifest.json").write_text(canonical(manifest) + "\n")
    load_catalog(OUT / "manifest.json")
    catalog = Path("benchmarks/comparison")
    catalog.mkdir(parents=True, exist_ok=True)
    (catalog / "cases.json").write_text(
        json.dumps(
            {
                "schema": 1,
                "manifest_hash": digest(manifest),
                "quotas": QUOTAS,
                "cases": cases,
                "excluded": dropped,
            },
            indent=2,
        )
        + "\n"
    )
    print(
        {
            "tasks": len(entries),
            "splits": {s: sum(e["split"] == s for e in entries) for s in ("learn", "dev", "test")},
            "manifest_hash": digest(manifest),
        }
    )


if __name__ == "__main__":
    main()
