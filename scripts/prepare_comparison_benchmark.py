"""Rebuild the checked-in 80-case catalog from pinned public commits, without model calls."""

import argparse
import json
import re
import subprocess
from pathlib import Path

from evocoder.contracts import canonical, digest
from mine_benchmark import build, git


def main(catalog, output, cache):
    if output.exists():
        raise ValueError("frozen output must be new")
    config = json.loads(catalog.read_text())
    built = []
    for case in config["cases"]:
        repository = case["repository"]
        if not re.fullmatch(r"[\w.-]+/[\w.-]+", repository):
            raise ValueError("invalid public repository")
        repo = cache / repository.replace("/", "--")
        if not (repo / ".git").exists():
            repo.mkdir(parents=True, exist_ok=True)
            subprocess.run(["git", "init", "--quiet", str(repo)], check=True)
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(repo),
                    "remote",
                    "add",
                    "origin",
                    "https://github.com/" + repository + ".git",
                ],
                check=True,
            )
        if git(repo, "remote", "get-url", "origin").strip() != "https://github.com/" + repository + ".git":
            raise ValueError("repository cache remote mismatch")
        for commit in (case["fixed_commit"], case["base_commit"]):
            exists = subprocess.run(
                ["git", "-C", str(repo), "cat-file", "-e", commit + "^{commit}"], capture_output=True
            )
            if exists.returncode:
                subprocess.run(
                    ["git", "-C", str(repo), "fetch", "--depth", "2", "origin", commit],
                    check=True,
                    timeout=120,
                )
        task, controller = build(repo, repository, case["fixed_commit"])
        if controller["base_commit"] != case["base_commit"]:
            raise ValueError("parent commit mismatch")
        original_hash = digest(task.model_dump())
        task = task.model_copy(update={"split": case["split"]})
        if digest(task.model_dump()) != case["task_hash"]:
            raise ValueError("reconstructed task hash mismatch")
        controller.update(
            {
                "pre_split_task_hash": original_hash,
                "task_hash": case["task_hash"],
                "split": case["split"],
                "timestamp": case["timestamp"],
            }
        )
        built.append((task, controller))
    entries = []
    for task, controller in built:
        for folder in ("tasks", "controller"):
            (output / folder).mkdir(parents=True, exist_ok=True)
        (output / "tasks" / f"{task.id}.json").write_text(canonical(task.model_dump()) + "\n")
        (output / "controller" / f"{task.id}.json").write_text(canonical(controller) + "\n")
        entries.append(
            {
                "id": task.id,
                "path": f"tasks/{task.id}.json",
                "hash": digest(task.model_dump()),
                "split": task.split,
                "repository": task.repository,
                "source_group": task.source_group,
                "timestamp": controller["timestamp"],
                "functions": controller["functions"],
                "family": task.repository.split("/")[1],
            }
        )
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
    if digest(manifest) != config["manifest_hash"]:
        raise ValueError("reconstructed manifest hash mismatch")
    (output / "manifest.json").write_text(canonical(manifest) + "\n")
    print({"tasks": len(entries), "manifest_hash": digest(manifest)})


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--catalog", type=Path, default=Path("benchmarks/comparison/cases.json"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--cache", type=Path, default=Path("work/benchmark-repos"))
    args = parser.parse_args()
    main(args.catalog, args.output, args.cache)
