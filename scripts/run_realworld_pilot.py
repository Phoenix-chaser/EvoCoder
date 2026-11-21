"""Freeze and run a small repository-disjoint pilot on the existing cumulative ledger."""
from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

from evocoder.contracts import Settings, digest
from evocoder.dataset import load_catalog
from evocoder.evolution import Evolution
from evocoder.runtime import Runner
from evocoder.sandbox import bounded_process, doctor
from evocoder.store import Store
from evocoder.stream import learn_stream


def dump(path, body):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(body, ensure_ascii=False, indent=2) + "\n")


def sources():
    paths = [*sorted(Path("src/evocoder").glob("*.py")), Path(__file__),
             Path("docker/realworld.Dockerfile"), Path("pyproject.toml"), Path("uv.lock")]
    return {str(p.relative_to(Path.cwd()) if p.is_absolute() else p): p.read_text() for p in paths}


def configured():
    image = bounded_process(["docker", "image", "inspect", "evocoder-realworld:py312-v1",
                             "--format", "{{.Id}}"], 10)
    if image["exit_code"] != 0 or image["timeout"]:
        raise ValueError("prepare the real-world task image first")
    return Settings().model_copy(update={"image": image["output"].strip()})


def prepare(root):
    settings = configured()
    settings.validate_live()
    store = Store(settings.state_dir)
    manifest = root / "bundle/manifest.json"
    verification = json.loads((root / "verification.json").read_text())
    if not verification["passed"] or verification["manifest_hash"] != digest(json.loads(manifest.read_text())):
        raise ValueError("offline defect/reference verification must pass before freezing")
    if {check["image_id"] for row in verification["rows"] for check in row["checks"].values()} != {settings.image}:
        raise ValueError("task image differs from offline verification image")
    image_id = doctor(settings)
    if not image_id["image_ready"] or store.active() is not None or store.budget()["reserved_usd"]:
        raise ValueError("requires a prepared image, no active skill and no pending model reservation")
    tasks = load_catalog(manifest)
    protocol = {"schema": 1, "claim": "exploratory_upstream_pr_reconstruction",
                "manifest_hash": digest(json.loads(manifest.read_text())),
                "verification_hash": digest(verification), "source_hash": digest(sources()),
                "settings": settings.public(), "budget_before": store.budget(),
                "initial_active": store.active(), "max_proposals": 2,
                "feedback_policy": "post_edit_or_budget", "repository_disjoint": True,
                "learn": [t.id for t in tasks if t.split == "learn"],
                "dev": [t.id for t in tasks if t.split == "dev"],
                "test": [t.id for t in tasks if t.split == "test"],
                "gate": "unchanged: two dev group wins, zero regressions or errors",
                "final_arms": ["no_skill", "first_frozen_skill", "latest_skill"],
                "identical_versions": "reuse the same run and mark aliases; not independent repetitions",
                "test_policy": "freeze versions before first held-out task; no test feedback learning",
                "not_implemented": ["trajectory RAG baseline", "repeated seeds", "official SWE-bench harness"],
                "limitations": ["six hand-selected PRs", "human issue summaries and public reproducers",
                                "historical code may be present in model training", "two tasks per split"]}
    if (root / "protocol.json").exists():
        raise ValueError("protocol already frozen")
    for path, content in sources().items():
        target = root / "source" / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    dump(root / "protocol.json", protocol)
    print(json.dumps(protocol, ensure_ascii=False, indent=2))


def execute(root):
    protocol = json.loads((root / "protocol.json").read_text())
    settings = configured()
    settings.validate_live()
    if settings.mode != "live" or settings.public() != protocol["settings"]:
        raise ValueError("live configuration differs from frozen protocol")
    manifest = root / "bundle/manifest.json"
    if digest(sources()) != protocol["source_hash"] or digest(json.loads(manifest.read_text())) != protocol["manifest_hash"]:
        raise ValueError("frozen code or dataset changed")
    if digest(json.loads((root / "verification.json").read_text())) != protocol["verification_hash"]:
        raise ValueError("offline verification receipt changed")
    tasks = load_catalog(manifest)
    store = Store(settings.state_dir)
    if store.budget() != protocol["budget_before"] or store.active() != protocol["initial_active"]:
        raise ValueError("ledger or active version changed since freeze")
    # Exclusive marker prevents accidentally rerunning a partially paid experiment.
    with (root / "started.json").open("x") as started:
        json.dump({"source_hash": protocol["source_hash"], "budget": store.budget()}, started)
    with store.connect() as db:
        before = {r[0] for r in db.execute("SELECT id FROM runs")}
    result = {"schema": 1, "mode": "live", "protocol": protocol, "completed": False}
    try:
        stream = learn_stream(settings, store, manifest, root / "stream", max_proposals=2,
                              feedback_policy="post_edit_or_budget")
        result["stream"] = stream
        candidates = [r["candidate"] for r in stream["timeline"] if r.get("candidate")]
        frozen = {"no_skill": None, "first_frozen_skill": candidates[0] if candidates else None,
                  "latest_skill": candidates[-1] if candidates else None}
        dump(root / "frozen-final-versions.json", frozen)
        lineage_count, active = len(Evolution(settings, store).lineage()), store.active()
        final = []
        if stream["completed"]:
            runner = Runner(settings, store)
            for task in tasks:
                if task.split != "test":
                    continue
                seen = {}
                for arm, skill_id in frozen.items():
                    alias = skill_id in seen
                    run_id = seen.get(skill_id) or runner.solve(task, skill_id, use_active=False, purpose="evaluate")
                    seen[skill_id] = run_id
                    run = store.run(run_id)
                    final.append({"task_id": task.id, "arm": arm, "skill_id": skill_id,
                                  "run_id": run_id, "alias_of_existing_run": alias, "state": run["state"],
                                  "passed": run["result"]["passed"], "cost": run["result"]["cost"]})
        if len(Evolution(settings, store).lineage()) != lineage_count or store.active() != active:
            raise ValueError("held-out evaluation changed frozen skill state")
        result.update({"completed": stream["completed"], "frozen_versions": frozen, "final_matrix": final,
                       "test_feedback_learning": False})
    except Exception as exc:
        error = f"{type(exc).__name__}: {str(exc)[:1200]}"
        result["error"] = error.replace(settings.api_key, "[redacted]")
        raise
    finally:
        with store.connect() as db:
            ids = [r[0] for r in db.execute("SELECT id FROM runs ORDER BY created") if r[0] not in before]
        result.update({"new_runs": [store.run(r) for r in ids], "budget_after": store.budget(),
                       "active": store.active()})
        dump(root / "result.json", result)
        for run_id in ids:
            events = store.events(run_id)
            dump(root / "traces" / f"{run_id}.json", {"run": store.run(run_id), "events": events})
            for event in events:
                if event["kind"] == "PATCH":
                    target = root / "patches" / f"{run_id}.patch"
                    target.parent.mkdir(exist_ok=True)
                    target.write_text(event["payload"]["diff"])
        shutil.copy2(root / "verification.json", root / "offline-verification.json")
    print(json.dumps({"completed": result["completed"], "active": result["active"],
                      "budget_after": result["budget_after"], "final_matrix": result.get("final_matrix", [])},
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path("experiments/realworld-003"))
    parser.add_argument("--run", action="store_true")
    args = parser.parse_args()
    execute(args.root) if args.run else prepare(args.root)
