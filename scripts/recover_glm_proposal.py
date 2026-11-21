"""New constrained proposal from a valid pilot repair; no repair or HTTP replay."""

import argparse
import json
from pathlib import Path

from evocoder.comparison import clone_state, dump, sources
from evocoder.contracts import Settings, digest
from evocoder.evolution import Evolution
from evocoder.store import Store


def execute(env: Path, prior: Path, root: Path):
    settings = Settings.from_env_file(env)
    settings.validate_live()
    if root.exists() or settings.accounting_unit != "credits":
        raise ValueError("new recovery destination and subscription credits required")
    previous = json.loads((prior / "result.json").read_text())
    prior_protocol = previous["protocol"]
    attempt = prior_protocol.get("proposal_attempt_including_prior", 1) + 1
    if attempt > 3:
        raise ValueError("at most three provider-contract proposal attempts, including the pilot")
    run_id = previous.get("run_id", prior_protocol.get("run_id"))
    ledger = Store(settings.state_dir, unit="credits")
    prior_store = Store(Path(prior_protocol["settings"]["state_dir"]), ledger=ledger, unit="credits")
    run = prior_store.run(run_id)
    if (
        previous["phase"] != "skill_proposal"
        or previous["completed"]
        or run["split"] != "learn"
        or run["state"] not in {"COMPLETE", "FAILED", "EXHAUSTED"}
        or ledger.budget()["reserved_credits"]
        or ledger.budget()["blocked"]
    ):
        raise ValueError("recovery requires a settled rejected proposal and valid frozen learn execution")
    root.mkdir(parents=True)
    store = clone_state(prior_store, root / "state", ledger)
    options = settings.model_copy(update={"state_dir": store.root})
    protocol = {
        "schema": 1,
        "claim": "provider_contract_recovery_only",
        "settings": options.public(),
        "ledger_root": str(ledger.root),
        "source_hash": digest(sources()),
        "prior_result_hash": digest(previous),
        "prior": str(prior.resolve()),
        "run_id": run_id,
        "task_hash": run["task_hash"],
        "no_repair_replay": True,
        "budget_before": ledger.budget(),
        "proposal_attempt_including_prior": attempt,
    }
    dump(root / "protocol.json", protocol)
    for name, body in sources().items():
        p = root / "source" / name
        p.parent.mkdir(exist_ok=True)
        p.write_text(body)
    (root / "runner.py").write_text(Path(__file__).read_text())
    with (root / "started.json").open("x") as marker:
        marker.write(json.dumps({"source_hash": protocol["source_hash"]}))
    result = {"completed": False, "phase": "skill_proposal", "protocol": protocol}
    try:
        key = Evolution(options, store, feedback_policy="post_edit_or_budget").propose([run_id])
        result.update(completed=True, phase="complete", skill_id=key)
        dump(root / "skill.json", store.get_artifact(key))
    except Exception as exc:
        result["error"] = (type(exc).__name__ + ": " + str(exc)[:1200]).replace(
            settings.api_key, "[redacted]"
        )
    finally:
        result["budget_after"] = ledger.budget()
        result["new_credits"] = round(
            ledger.budget()["spent_credits"] - protocol["budget_before"]["spent_credits"], 9
        )
        dump(root / "result.json", result)
    print(
        json.dumps(
            {k: result.get(k) for k in ["completed", "error", "skill_id", "budget_after", "new_credits"]},
            ensure_ascii=False,
        )
    )
    return 0 if result["completed"] else 2


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--env-file", type=Path, default=Path(".env.glm"))
    p.add_argument("--prior", type=Path, default=Path("experiments/glm-pilot-001"))
    p.add_argument("--output", type=Path, default=Path("experiments/glm-proposal-recovery-001"))
    a = p.parse_args()
    raise SystemExit(execute(a.env_file, a.prior, a.output))
