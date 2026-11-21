"""One-shot GLM contract probe + a declared learn defect; never uses test tasks."""

import argparse
import json
from pathlib import Path

from evocoder.comparison import dump, sources
from evocoder.contracts import Action, Settings, digest
from evocoder.dataset import load_catalog
from evocoder.evolution import Evolution
from evocoder.model import Model
from evocoder.runtime import Runner
from evocoder.store import Store, uid


def execute(env: Path, root: Path, manifest: Path):
    options = Settings.from_env_file(env)
    options.validate_live()
    if options.provider != "glm" or options.billing_mode != "subscription_credits":
        raise ValueError("this pilot requires the separately configured GLM Coding Plan")
    if root.exists():
        raise ValueError("pilot destination must be new; no retry or overwrite")
    if options.state_dir.resolve() != (root / "state").resolve():
        raise ValueError("pilot requires its own isolated state and credit ledger")
    task = next(t for t in load_catalog(manifest) if t.id == "tkem-cachetools-d3598664ca")
    if task.split != "learn":
        raise ValueError("probe task must be learn")
    root.mkdir(parents=True)
    protocol = {
        "schema": 1,
        "claim": "provider_and_execution_validation_only",
        "settings": options.public(),
        "manifest": str(manifest.resolve()),
        "manifest_hash": digest(json.loads(manifest.read_text())),
        "source_hash": digest(sources()),
        "task_id": task.id,
        "task_hash": digest(task.model_dump()),
        "billing_unit": "credits",
        "actual_subscription_cost_usd": None,
        "local_credit_cap": None if options.unlimited_credits else options.credit_budget,
        "no_final_test": True,
        "no_automatic_retry": True,
    }
    dump(root / "protocol.json", protocol)
    for name, body in sources().items():
        path = root / "source" / name
        path.parent.mkdir(exist_ok=True)
        path.write_text(body)
    (root / "runner.py").write_text(Path(__file__).read_text())
    with (root / "started.json").open("x") as marker:
        marker.write(json.dumps({"source_hash": protocol["source_hash"]}))
    store = Store(options.state_dir, unit="credits")
    result = {"protocol": protocol, "completed": False, "phase": "contract_probe"}
    try:
        model = Model(options, store)
        probe = uid()
        result["probe_id"] = probe
        decision = model.request(
            Action,
            "Validate a coding-agent tool connection. Return the finish action with a short reason.",
            {"purpose": "coding tool contract check; no source files or shell commands"},
            probe,
        )
        if decision.tool != "finish":
            raise ValueError("probe did not return the requested finish action; no tool executed")
        dump(
            root / "probe.json",
            {"action": decision.model_dump(), "events": store.events(probe), "cost": store.costs([probe])},
        )
        result["phase"] = "learn_repair"
        run_id = Runner(options, store).solve(task, use_active=False)
        result["run_id"] = run_id
        run = store.run(run_id)
        result["run"] = run
        dump(root / "trace.json", {"run": run, "events": store.events(run_id)})
        if run["state"] not in {"COMPLETE", "FAILED", "EXHAUSTED"}:
            raise RuntimeError("learn execution error; stop without replay")
        result["phase"] = "skill_proposal"
        key = Evolution(options, store, feedback_policy="post_edit_or_budget").propose([run_id])
        result["skill_id"] = key
        dump(root / "skill.json", store.get_artifact(key))
        result.update(completed=True, phase="complete")
    except Exception as exc:
        result["error"] = (type(exc).__name__ + ": " + str(exc)[:1200]).replace(options.api_key, "[redacted]")
    finally:
        result["budget_after"] = store.budget()
        dump(root / "result.json", result)
    print(
        json.dumps(
            {k: result.get(k) for k in ["completed", "phase", "error", "run_id", "skill_id", "budget_after"]},
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if result["completed"] else 2


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--env-file", type=Path, default=Path(".env.glm"))
    p.add_argument("--output", type=Path, default=Path("experiments/glm-pilot-001"))
    p.add_argument(
        "--manifest", type=Path, default=Path("experiments/comparison-benchmark-001/manifest.json")
    )
    args = p.parse_args()
    raise SystemExit(execute(args.env_file, args.output, args.manifest))
