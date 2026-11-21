"""Validate the new content-only proposal contract once, without replaying a repair."""

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
    previous = json.loads((prior / "result.json").read_text())
    old = previous["protocol"]
    if (
        root.exists()
        or settings.accounting_unit != "credits"
        or previous["completed"]
        or previous["phase"] != "skill_proposal"
        or old.get("proposal_attempt_including_prior") != 3
        or old["source_hash"] == digest(sources())
    ):
        raise ValueError("requires three retained rejected legacy proposals and a new contract/source")
    ledger = Store(settings.state_dir, unit="credits")
    before = ledger.budget()
    if before["reserved_credits"] or before["blocked"]:
        raise ValueError("unsettled ledger; no automatic request replay")
    original = Store(Path(old["settings"]["state_dir"]), ledger=ledger, unit="credits")
    run_id = old["run_id"]
    run = original.run(run_id)
    if run["split"] != "learn" or run["state"] != "COMPLETE":
        raise ValueError("requires the completed pilot repair, no new repair or final test")
    root.mkdir(parents=True)
    store = clone_state(original, root / "state", ledger)
    options = settings.model_copy(update={"state_dir": store.root})
    protocol = {
        "schema": 2,
        "claim": "new_content_contract_validation_only",
        "proposal_contract": "runtime_owned_skill_content_v2",
        "settings": options.public(),
        "ledger_root": str(ledger.root),
        "source_hash": digest(sources()),
        "prior": str(prior.resolve()),
        "prior_result_hash": digest(previous),
        "legacy_rejected_attempts": 3,
        "new_contract_max_attempts": 1,
        "run_id": run_id,
        "task_hash": run["task_hash"],
        "no_repair_replay": True,
        "no_final_test": True,
        "budget_before": before,
    }
    dump(root / "protocol.json", protocol)
    for name, body in sources().items():
        p = root / "source" / name
        p.parent.mkdir(exist_ok=True)
        p.write_text(body)
    (root / "runner.py").write_text(Path(__file__).read_text())
    with (root / "started.json").open("x") as marker:
        marker.write(json.dumps({"source_hash": protocol["source_hash"]}))
    dump(root / "trace.json", {"run": run, "events": store.events(run_id)})
    result = {"completed": False, "phase": "skill_proposal", "protocol": protocol, "run_id": run_id}
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
        result["new_credits"] = round(ledger.budget()["spent_credits"] - before["spent_credits"], 9)
        dump(root / "result.json", result)
    print(json.dumps({k: v for k, v in result.items() if k != "protocol"}, ensure_ascii=False))
    return 0 if result["completed"] else 2


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--env-file", type=Path, default=Path(".env.glm"))
    p.add_argument("--prior", type=Path, default=Path("experiments/glm-proposal-recovery-002"))
    p.add_argument("--output", type=Path, default=Path("experiments/glm-content-pilot-001"))
    a = p.parse_args()
    raise SystemExit(execute(a.env_file, a.prior, a.output))
