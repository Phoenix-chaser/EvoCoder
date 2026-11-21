"""Second paid pilot: frozen same-domain cache contracts, one shared cumulative budget."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from cache_suite import HELPERS, specs
from evocoder.contracts import Settings, Task, canonical, digest
from evocoder.dataset import load_catalog
from evocoder.evolution import Evolution
from evocoder.runtime import Runner, grade, verify_public
from evocoder.store import Store
from evocoder.stream import learn_stream

ROOT = Path("experiments/live-002")
SEED = "8e1999dda10186753f8eff01768cd9145bcab2fdab01fea35308f02f31478f80"


def dump(path, body):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(body, ensure_ascii=False, indent=2) + "\n")


def source_hash():
    paths = [*sorted(Path("src/evocoder").glob("*.py")), Path("scripts/cache_suite.py"),
             Path("scripts/run_cache_pilot.py")]
    return digest({str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths})


def prepare():
    settings = Settings()
    destination = ROOT / "dataset"
    destination.mkdir(parents=True, exist_ok=True)
    entries, validations = [], []
    for spec in specs():
        task = Task(id=spec["id"], source_group=spec["group"], repository="cache-contracts/" + spec["group"],
                    split=spec["split"], issue=spec["issue"],
                    files={spec["module"]: spec["buggy"], **spec.get("extra", {}),
                           "test_public.py": HELPERS + "\n" + spec["public"]},
                    editable=[spec["module"]], public_command=["python", "-m", "unittest", "-v", "test_public"],
                    grader_files={"test_private.py": HELPERS + "\n" + spec["hidden"]},
                    grader_command=["python", "-m", "unittest", "-v", "test_public", "test_private"])
        path = destination / (task.id + ".json")
        content = canonical(task.model_dump()) + "\n"
        if path.exists() and path.read_text() != content:
            raise ValueError("frozen task content changed")
        path.write_text(content)
        correct = {spec["module"]: spec["fixed"]}
        public_result = verify_public(task, correct, settings, "reference-public-" + task.id)
        reference = grade(task, correct, settings, "reference-private-" + task.id)
        original = verify_public(task, {spec["module"]: spec["buggy"]}, settings, "original-" + task.id)
        row = {"task_id": task.id, "reference_public_pass": public_result["exit_code"] == 0,
               "reference_grade_pass": reference["passed"], "original_public_fails": original["exit_code"] != 0,
               "image_id": reference["image_id"]}
        validations.append(row)
        print(canonical(row), flush=True)
        if not row["reference_public_pass"] or not row["reference_grade_pass"]:
            dump(ROOT / "reference-errors.json", {"task": task.id, "public": public_result,
                                                  "private": reference})
            raise ValueError("reference validation failed; no paid calls allowed")
        if not row["original_public_fails"]:
            raise ValueError("public tests must expose the original defect")
        entries.append({"path": path.name, "hash": digest(task.model_dump()), "id": task.id,
                        "split": task.split, "source_group": task.source_group})
    manifest = {"schema": 1, "kind": "manually_authored_cache_contracts",
                "known_to_developer": True, "natural_repository_issues": False,
                "tasks": entries}
    path = destination / "manifest.json"
    if path.exists() and json.loads(path.read_text()) != manifest:
        raise ValueError("frozen manifest changed")
    dump(path, manifest)
    dump(ROOT / "offline-validation.json", validations)
    store = Store(settings.state_dir)
    protocol = {"schema": 1, "model": settings.model, "budget_before": store.budget(),
                "state_dir": str(settings.state_dir), "same_cumulative_cap_usd": settings.budget_usd,
                "source_hash": source_hash(), "manifest_hash": digest(manifest),
                "seed_candidate": SEED, "initial_active": store.active(), "max_proposals": 2,
                "max_steps": settings.max_steps, "image": settings.image,
                "failure_source": "fresh frozen public-suite validation after patch submission",
                "hidden_feedback_to_learning": False, "automatic_activation": "unchanged quality gate",
                "final_arms": ["no_skill", "frozen_seed", "latest_revision"],
                "final_revision_selection": "last proposed descendant, or seed if none; independent of test scores",
                "duplicate_bundle_arms": "alias identical bundle to same execution; not independent repeats",
                "quality_gate": "2 improved dev source groups, no observed regressions or infrastructure errors",
                "limitations": ["self-built tasks", "small sample", "shared cache concepts",
                                "no statistical independence claim", "not SWE-bench", "not four-arm study"]}
    if (ROOT / "protocol.json").exists():
        raise ValueError("protocol already frozen; use existing assets without overwriting")
    dump(ROOT / "protocol.json", protocol)


def execute():
    settings = Settings()
    settings.validate_live()
    if settings.mode != "live":
        raise ValueError("explicit live configuration required")
    protocol = json.loads((ROOT / "protocol.json").read_text())
    if source_hash() != protocol["source_hash"]:
        raise ValueError("runtime or dataset generator changed after freezing")
    if digest(json.loads((ROOT / "dataset/manifest.json").read_text())) != protocol["manifest_hash"]:
        raise ValueError("manifest changed after freezing")
    store = Store(settings.state_dir)
    if store.active() != protocol["initial_active"]:
        raise ValueError("active changed since protocol freeze")
    with store.connect() as db:
        before_run_ids = {r[0] for r in db.execute("SELECT id FROM runs")}
        before_skill_ids = {r[0] for r in db.execute("SELECT id FROM skills")}
        before_call_ids = {r[0] for r in db.execute("SELECT id FROM calls")}
    dump(ROOT / "before.json", {"runs": sorted(before_run_ids), "skills": sorted(before_skill_ids),
                                "calls": sorted(before_call_ids), "budget": store.budget()})
    report = learn_stream(settings, store, ROOT / "dataset/manifest.json", ROOT / "stream",
                          max_proposals=2, with_final_test=False, initial_candidate=SEED)
    if not report["completed"]:
        raise RuntimeError("stream aborted; inspect stored failures; no final matrix executed")
    timeline = report["timeline"]
    candidates = [t["candidate"] for t in timeline if t.get("candidate")]
    latest = candidates[-1] if candidates else SEED
    arms = {"no_skill": None, "frozen_seed": SEED, "latest_revision": latest}
    frozen_active = store.active()
    frozen_lineage = len(Evolution(settings, store).lineage())
    finals = []
    runner = Runner(settings, store)
    for task in load_catalog(ROOT / "dataset/manifest.json"):
        if task.split != "test":
            continue
        aliases = {}
        for arm, bundle in arms.items():
            # Same bundle has no distinct evolution state; do not manufacture another trial.
            if bundle in aliases:
                run_id = aliases[bundle]
                alias_of = next(a for a in arms if a != arm and arms[a] == bundle)
            else:
                run_id = runner.solve(task, bundle, use_active=False, purpose="evaluate")
                aliases[bundle] = run_id
                alias_of = None
            run = store.run(run_id)
            finals.append({"task_id": task.id, "arm": arm, "skill_id": bundle,
                           "run_id": run_id, "alias_of": alias_of, "state": run["state"],
                           "passed": run["result"]["passed"], "cost": run["result"]["cost"]})
    if store.active() != frozen_active or len(Evolution(settings, store).lineage()) != frozen_lineage:
        raise ValueError("final matrix mutated strategy state")
    with store.connect() as db:
        ids = [r[0] for r in db.execute("SELECT id FROM runs ORDER BY created") if r[0] not in before_run_ids]
        call_ids = [r[0] for r in db.execute("SELECT id FROM calls") if r[0] not in before_call_ids]
    summary = {"schema": 1, "mode": "live", "claim": "exploratory_same_domain_selfbuilt_tasks",
               "protocol": protocol, "timeline": timeline, "final_matrix": finals,
               "new_skill_ids": candidates, "latest_revision": latest, "active": store.active(),
               "budget_before": protocol["budget_before"], "budget_after": store.budget(),
               "new_call_count": len(call_ids), "new_runs": [store.run(i) for i in ids],
               "new_skills": [e for e in Evolution(settings, store).lineage() if e["id"] not in before_skill_ids],
               "final_learning": False}
    dump(ROOT / "summary.json", summary)
    for run_id in ids:
        dump(ROOT / "traces" / f"{run_id}.json", {"run": store.run(run_id), "events": store.events(run_id)})
    print(json.dumps({"new_candidates": candidates, "active": store.active(), "final_matrix": finals,
                      "budget": store.budget(), "calls": len(call_ids)}, ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--run", action="store_true")
    args = parser.parse_args()
    execute() if args.run else prepare()
