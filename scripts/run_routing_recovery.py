"""Predeclared engineering ablation; no new skill, promotion or held-out-test claims."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from evocoder.contracts import Settings, Skill, digest
from evocoder.dataset import load_catalog
from evocoder.runtime import Runner
from evocoder.sandbox import bounded_process
from evocoder.store import Store

SKILL = "dde192eca5172b2a8a6a9ba2882b814318c93b83ada75468ca03c13c65efd979"
MANIFEST = Path("experiments/realworld-003/bundle/manifest.json")
ARMS = {
    "force_stop": ("off", False, SKILL),
    "force_recover": ("off", True, SKILL),
    "route_stop": ("semantic", False, SKILL),
    "route_recover": ("semantic", True, SKILL),
    "no_skill_recover": ("off", True, None),
}


def dump(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def sources():
    paths = [*sorted(Path("src/evocoder").glob("*.py")), Path(__file__).resolve(),
             Path("pyproject.toml"), Path("uv.lock")]
    return {str(p.relative_to(Path.cwd()) if p.is_absolute() else p): p.read_text() for p in paths}


def settings():
    image = bounded_process(["docker", "image", "inspect", "evocoder-realworld:py312-v1",
                             "--format", "{{.Id}}"], 10)
    if image["exit_code"] != 0 or image["timeout"]:
        raise ValueError("prepare the real-world image first")
    return Settings().model_copy(update={"image": image["output"].strip()})


def prepare(root):
    if root.exists():
        raise ValueError("use a new experiment directory")
    options = settings()
    options.validate_live()
    store = Store(options.state_dir)
    skill = Skill.model_validate(store.get_artifact(SKILL))
    if skill.hash != SKILL or store.budget()["reserved_usd"] or store.active() is not None:
        raise ValueError("skill, budget or active-version precondition failed")
    tasks = load_catalog(MANIFEST)
    protocol = {"schema": 1, "claim": "reused_development_engineering_ablation",
                "source_hash": digest(sources()), "settings": options.public(),
                "manifest_hash": digest(json.loads(MANIFEST.read_text())), "skill_id": SKILL,
                "budget_before": store.budget(), "initial_active": store.active(),
                "dev_tasks": [t.id for t in tasks if t.split == "dev"],
                "positive_routing_probe": "dotenv-empty-cache", "arms": ARMS,
                "selection": "all original Click dev cases, no exclusion after outcomes",
                "order": "rotate arm order by task index", "model_step_budget": options.max_steps,
                "routing_step_cost": "one model step within max_steps, included in cost",
                "test_tasks": [], "new_skills": False, "publish": False,
                "interpretation": "current-runner factorial ablation, not reproduction of old binary or learning gain",
                "expectation": "cache skill should be skipped on both Click issues and applied on empty-cache probe"}
    root.mkdir(parents=True)
    for path, body in sources().items():
        target = root / "source" / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body)
    dump(root / "protocol.json", protocol)
    print(json.dumps(protocol, ensure_ascii=False, indent=2))


def execute(root):
    protocol = json.loads((root / "protocol.json").read_text())
    options = settings()
    options.validate_live()
    store = Store(options.state_dir)
    if options.mode != "live" or options.public() != protocol["settings"] or digest(sources()) != protocol["source_hash"]:
        raise ValueError("live configuration or source changed after freeze")
    if digest(json.loads(MANIFEST.read_text())) != protocol["manifest_hash"] or digest(store.get_artifact(SKILL)) != SKILL:
        raise ValueError("task assets or frozen skill changed")
    if store.budget() != protocol["budget_before"] or store.active() != protocol["initial_active"]:
        raise ValueError("ledger or active version changed after freeze")
    with (root / "started.json").open("x") as started:
        json.dump({"budget": store.budget()}, started)
    with store.connect() as db:
        before_ids = {r[0] for r in db.execute("SELECT id FROM runs")}
        skill_ids = {r[0] for r in db.execute("SELECT id FROM skills")}
        release_count = db.execute("SELECT COUNT(*) FROM releases").fetchone()[0]
    result = {"schema": 1, "protocol": protocol, "rows": [], "completed": False}
    tasks = load_catalog(MANIFEST)
    try:
        dev = [t for t in tasks if t.id in protocol["dev_tasks"]]
        for index, task in enumerate(dev):
            arms = list(ARMS)
            for arm in arms[index:] + arms[:index]:
                route, recover, skill = ARMS[arm]
                config = options.model_copy(update={"skill_routing": route, "recover_actions": recover})
                run_id = Runner(config, store).solve(task, skill, use_active=False, purpose="evaluate")
                run = store.run(run_id)
                result["rows"].append({"task_id": task.id, "arm": arm, "run_id": run_id,
                                       "state": run["state"], **run["result"]})
                dump(root / "progress.json", result["rows"])
                if store.budget()["reserved_usd"] or store.budget()["blocked"]:
                    raise RuntimeError("unsettled model response or sealed budget; stopping without retry")
        probe = next(t for t in tasks if t.id == protocol["positive_routing_probe"])
        run_id = Runner(options.model_copy(update={"skill_routing": "semantic", "recover_actions": True}),
                        store).solve(probe, SKILL, use_active=False, purpose="evaluate")
        result["positive_probe"] = store.run(run_id)
        result["completed"] = True
    except Exception as exc:
        message = f"{type(exc).__name__}: {str(exc)[:1200]}"
        result["error"] = message.replace(options.api_key, "[redacted]")
        raise
    finally:
        with store.connect() as db:
            ids = [r[0] for r in db.execute("SELECT id FROM runs ORDER BY created") if r[0] not in before_ids]
            after_skills = {r[0] for r in db.execute("SELECT id FROM skills")}
            after_releases = db.execute("SELECT COUNT(*) FROM releases").fetchone()[0]
        result.update({"new_runs": [store.run(r) for r in ids], "budget_after": store.budget(),
                       "active": store.active(), "skill_mutation": after_skills != skill_ids,
                       "release_mutation": after_releases != release_count})
        dump(root / "result.json", result)
        for run_id in ids:
            events = store.events(run_id)
            dump(root / "traces" / f"{run_id}.json", {"run": store.run(run_id), "events": events})
            for event in events:
                if event["kind"] == "MODEL_ARGUMENTS_REJECTED":
                    key = event["payload"]["artifact"]
                    dump(root / "rejected-arguments" / f"{key}.json", store.get_artifact(key))
                if event["kind"] == "PATCH":
                    patch = root / "patches" / f"{run_id}.patch"
                    patch.parent.mkdir(exist_ok=True)
                    patch.write_text(event["payload"]["diff"])
        if result["skill_mutation"] or result["release_mutation"] or store.active() != protocol["initial_active"]:
            raise RuntimeError("ablation changed frozen skill or release state")
    print(json.dumps({"completed": result["completed"], "budget": result["budget_after"],
                      "rows": len(result["rows"])}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path("experiments/optimization-001"))
    parser.add_argument("--run", action="store_true")
    args = parser.parse_args()
    execute(args.root) if args.run else prepare(args.root)
