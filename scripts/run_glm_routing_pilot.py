"""Three one-shot live routing probes from existing learn inputs; no repair replay."""

import argparse
import json
import sqlite3
from pathlib import Path

from evocoder.comparison import dump, sources
from evocoder.contracts import Settings, Skill, digest
from evocoder.dataset import load_catalog
from evocoder.model import Model
from evocoder.routing import select_library_skill
from evocoder.store import Store, uid


CACHE = "d82c10cf110b4335fbff3fc9cf74c72aa75b3e98a0a4d4b68d19d5c2cfcf2ecb"
CLI = "3fb0dd32755148b8982ed5d61cd5cd91c7a4ccea8188b257edeb76f67ec9f887"


def execute(env: Path, prior: Path, root: Path, manifest: Path):
    settings = Settings.from_env_file(env)
    settings.validate_live()
    previous = json.loads((prior / "result.json").read_text())
    if root.exists() or settings.accounting_unit != "credits" or previous["completed"] or previous["rows"]:
        raise ValueError("requires new output and a stopped pre-test credit study")
    ledger = Store(settings.state_dir, unit="credits")
    before = ledger.budget()
    if before["reserved_credits"] or before["blocked"]:
        raise ValueError("wait for all dispatched requests to settle before contract validation")
    with sqlite3.connect(f"file:{(prior / 'seed/evocoder.sqlite3').resolve()}?mode=ro", uri=True) as db:
        skills = {
            key: Skill.model_validate(
                json.loads(db.execute("SELECT body FROM artifacts WHERE id=?", (key,)).fetchone()[0])
            )
            for key in [CACHE, CLI]
        }
    assert all(skill.hash == key and skill.parent is None for key, skill in skills.items())
    tasks = {t.id: t for t in load_catalog(manifest)}
    cache = tasks["tkem-cachetools-d3598664ca"]
    cli = tasks["pallets-click-42ee8a1f60"]
    assert cache.split == cli.split == "learn"
    cases = [
        ("unrelated", cli, [CACHE], None),
        ("single_apply", cache, [CACHE], CACHE),
        ("choose_one", cli, [CACHE, CLI], CLI),
    ]
    root.mkdir(parents=True)
    store = Store(root / "state", ledger=ledger, unit="credits")
    options = settings.model_copy(update={"state_dir": store.root})
    protocol = {
        "schema": 1,
        "claim": "integer_routing_contract_validation_only",
        "settings": options.public(),
        "ledger_root": str(ledger.root),
        "source_hash": digest(sources()),
        "prior_result_hash": digest(previous),
        "prior": str(prior.resolve()),
        "budget_before": before,
        "no_repair_replay": True,
        "no_final_test": True,
        "max_calls": 3,
        "cases": [
            {
                "name": name,
                "task_id": task.id,
                "task_hash": digest(task.model_dump()),
                "skills": ids,
                "expected_id": expected,
            }
            for name, task, ids, expected in cases
        ],
    }
    dump(root / "protocol.json", protocol)
    dump(root / "skill.json", {k: s.model_dump() for k, s in skills.items()})
    for name, body in sources().items():
        p = root / "source" / name
        p.parent.mkdir(exist_ok=True)
        p.write_text(body)
    (root / "runner.py").write_text(Path(__file__).read_text())
    with (root / "started.json").open("x") as f:
        f.write(json.dumps({"source_hash": protocol["source_hash"]}))
    records = []
    result = {"completed": False, "phase": "routing_probe", "protocol": protocol}
    try:
        model = Model(options, store)
        for name, task, ids, expected in cases:
            run_id = uid()
            record = {"name": name, "run_id": run_id}
            records.append(record)
            try:
                decision = select_library_skill(model, task, {key: skills[key] for key in ids}, run_id)
                record["decision"] = decision
                if decision["skill_id"] != expected or decision["applied"] != bool(expected):
                    raise ValueError("valid route did not match the predeclared positive/negative case")
                record["passed"] = True
            finally:
                record["events"] = store.events(run_id)
                record["cost"] = store.costs([run_id])
                dump(root / "probe.json", records)
        result.update(completed=True, phase="complete")
    except Exception as exc:
        result["error"] = (type(exc).__name__ + ": " + str(exc)[:1200]).replace(
            settings.api_key, "[redacted]"
        )
    finally:
        result["budget_after"] = ledger.budget()
        result["new_credits"] = round(ledger.budget()["spent_credits"] - before["spent_credits"], 9)
        result["passed_probes"] = sum(bool(r.get("passed")) for r in records)
        dump(root / "result.json", result)
    print(json.dumps({k: v for k, v in result.items() if k != "protocol"}, ensure_ascii=False))
    return 0 if result["completed"] else 2


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--env-file", type=Path, default=Path(".env.glm"))
    p.add_argument("--prior", type=Path, default=Path("experiments/comparison-glm-001"))
    p.add_argument("--output", type=Path, default=Path("experiments/glm-routing-pilot-001"))
    p.add_argument(
        "--manifest", type=Path, default=Path("experiments/comparison-benchmark-001/manifest.json")
    )
    a = p.parse_args()
    raise SystemExit(execute(a.env_file, a.prior, a.output, a.manifest))
