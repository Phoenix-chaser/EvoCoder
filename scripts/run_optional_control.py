"""Freeze and run the GLM positive control. The reference and truthiness mutant are evaluator-only."""
import argparse
import fcntl
import json
import threading
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from evocoder.comparison import dump, sources
from evocoder.contracts import Settings, Skill, digest
from evocoder.dataset import load_catalog
from evocoder.errors import ProposalRejected
from evocoder.evolution import Evolution
from evocoder.gate import gate_summary
from evocoder.jobs import bounded_map
from evocoder.library import SkillLibrary
from evocoder.optional_benchmark import calibrate, catalog, freeze_dataset
from evocoder.runtime import Runner
from evocoder.store import Store

ROOTS = ["dispatch-01", "export-02", "dispatch-03", "export-04"]
ANCHORS = ["gateway-01", "audit-02"]
FAMILIES = ["scalar", "layers", "batch"]
ORACLE = {
    "name": "preserve-explicit-configuration",
    "applicability": ["An optional client setting overrides a preset."],
    "steps": ["Follow the setting from the public entry point to its output.",
              "Use the contract's absence sentinel to select a default. Preserve supplied zeros, False and empty containers."],
    "verification": ["Exercise missing, null, a valid falsy override and a normal override at the public boundary."],
    "exclusions": ["The contract explicitly defines empty input as missing."], "counterexamples": [],
    "evidence_refs": ["evaluator-authored-control"], "change_summary": "Independent evaluator-authored positive control.",
    "parent": None,
}


def read(path):
    return json.loads(path.read_text())


def prepare(root, settings):
    root.mkdir(parents=True, exist_ok=True)
    if (root / "protocol.json").exists():
        raise ValueError("protocol already frozen; use run to resume")
    if not (root / "dataset/manifest.json").exists():
        freeze_dataset(root / "dataset")
    manifest = read(root / "dataset/manifest.json")
    expected = catalog()
    tasks = load_catalog(root / "dataset/manifest.json")
    if [digest(t.model_dump()) for t in tasks] != [digest(t.model_dump()) for _, t, _, _ in expected]:
        raise ValueError("dataset differs from generator")
    if not (root / "calibration.json").exists():
        with ThreadPoolExecutor(4) as pool:
            cal = list(pool.map(lambda item: calibrate(item, settings), expected))
        dump(root / "calibration.json", {"manifest": digest(manifest), "rows": cal})
    cal = read(root / "calibration.json")
    if cal["manifest"] != digest(manifest) or len(cal["rows"]) != len(tasks):
        raise ValueError("unbound calibration")
    for row, task in zip(cal["rows"], tasks, strict=True):
        if (row["hash"] != digest(task.model_dump()) or row["image"] != settings.image
                or row["exit_codes"] != [[1, 1], [0, 0], [int(task.split == "learn"), 1]]):
            raise ValueError("failed reference or mutant calibration")
    proto = {"schema": 1, "study": "optional-configuration-control-v1", "manifest": digest(manifest),
             "calibration": digest(cal), "source": digest(sources()), "driver": digest(Path(__file__).read_text()),
             "settings_hash": digest(settings.public()), "model": settings.model, "image": settings.image,
             "learning_seed": ROOTS, "learning_trials": [t.id for t in tasks if t.split == "learn" and t.id not in ROOTS],
             "candidate_policy": "one learned root; at most one child from up to four actually applied qualifying later failures",
             "uptake_report": digest(read(Path("docs/results/behavior-uptake.json"))),
             "anchors": ANCHORS, "anchor_repeats": 2,
             "screen": "every dev case once; stop only when the existing two-repeat gate is mathematically unreachable",
             "release": "standard full-library gate, all ten dev cases twice and all cited learning support once",
             "oracle": digest(ORACLE), "oracle_policy": "on a negative screen, fixed evaluator-only procedure once per anchor",
             "holdout": "only after publication; one paired trial on each of 30 untouched tasks; no learning or version changes",
             "family_order": FAMILIES, "curve": "equal-weight cumulative family means of paired repair differences; also report each family",
             "max_steps": settings.max_steps, "solver_slots_per_arm": settings.max_steps - 1,
             "workers": 2, "model_task_input": "normal allowlisted task view; grader, reference, oracle and split are withheld"}
    dump(root / "protocol.json", proto)
    return proto


class Study:
    def __init__(self, root, settings, store, ledger, protocol):
        self.root, self.settings, self.store, self.ledger = root, settings, store, ledger
        self.protocol = digest(protocol)
        self.tasks = {t.id: t for t in load_catalog(root / "dataset/manifest.json")}
        self.oracle_store = None

    def solve(self, task, phase, repeat=0, *, pool=None, oracle=False, learning=False):
        # A no-skill/forced-oracle run reserves the same single routing slot and gets 15 solver actions.
        options = self.settings if pool else self.settings.model_copy(update={"max_steps": self.settings.max_steps - 1})
        st = self.store
        kwargs = {"use_active": False, "purpose": "solve" if learning else "evaluate", "candidate": learning}
        if oracle:
            options = options.model_copy(update={"skill_routing": "off"})
            st = self.oracle_store
            kwargs["skill_id"] = self.oracle_key
        elif pool:
            kwargs["skill_ids"] = pool
        identity = {"protocol": self.protocol, "phase": phase, "task": task.id, "task_hash": digest(task.model_dump()),
                    "repeat": repeat, "pool": pool or [], "oracle": oracle, "options": digest(options.public())}
        key = digest(identity)[:32]
        slot = self.root / "slots" / (key + ".json")
        if slot.exists() and read(slot) != identity:
            raise ValueError("changed execution intent")
        if not slot.exists():
            dump(slot, identity)
        with st.connect() as db:
            existing = db.execute("SELECT state FROM runs WHERE id=?", (key,)).fetchone()
        if existing is None:
            Runner(options, st).solve(task, run_id=key, **kwargs)
        run = st.run(key)
        if run["state"] not in ("COMPLETE", "FAILED", "EXHAUSTED"):
            raise RuntimeError(f"execution {key} stopped in {run['state']}: {(run['result'] or {}).get('error', '')}")
        if run["task_hash"] != identity["task_hash"] or run["mode"] != "live":
            raise ValueError("execution differs from frozen task")
        return key

    def proposal(self, label, runs, parent=None):
        receipt = self.root / (label + ".json")
        intent = {"protocol": self.protocol, "runs": runs, "parent": parent}
        if receipt.is_file():
            record = read(receipt)
            if record["intent"] != intent:
                raise ValueError("proposal evidence changed")
            if record.get("skill"):
                self.store.get_artifact(record["skill"])
                return record["skill"]
            if record.get("rejected"):
                return None
            raise ValueError("unfinished proposal intent; inspect its original execution before issuing another")
        dump(receipt, {"intent": intent})
        engine = Evolution(self.settings, self.store, feedback_policy="learn_outcome", proposal_policy="standard")
        try:
            key = engine.propose(runs, parent)
        except ProposalRejected:
            dump(receipt, {"intent": intent, "rejected": True})
            return None
        dump(receipt, {"intent": intent, "skill": key})
        return key

    def oracle(self):
        if self.oracle_store is None:
            self.oracle_store = Store(self.root / "oracle-state", ledger=self.ledger)
            artifact = Skill.model_validate(ORACLE).model_dump()
            self.oracle_key = self.oracle_store.artifact("skill", artifact)
            with self.oracle_store.connect(write=True) as db:
                db.execute("INSERT OR IGNORE INTO skills(id,parent,status,evidence_hash) VALUES(?,NULL,?,?)",
                           (self.oracle_key, "CANDIDATE", digest("evaluator-only")))
        return [self.oracle_store.run(self.solve(self.tasks[t], "oracle", oracle=True))["result"]["passed"] for t in ANCHORS]


class StudyLibrary(SkillLibrary):
    """Use normal routing and gate, reusing only the identical predeclared task/arm/repeat attempt."""
    def __init__(self, study):
        super().__init__(study.settings, study.store)
        self.study = study

    def compare_pair(self, task, repeat, baseline, variants, candidate, *, reverse, stop, fixed_route=None):
        study = self.study
        phase = "holdout" if task.split == "test" else "gate"

        class Attempts:
            def solve(self, task, *, use_active, purpose, skill_ids):
                return study.solve(task, phase, repeat, pool=skill_ids)

        paired = SkillLibrary(self.settings, self.store, runner=Attempts())
        return paired.compare_pair(task, repeat, baseline, variants, candidate, reverse=reverse, stop=stop)


def execute(root, settings):
    p = read(root / "protocol.json")
    if (p["driver"] != digest(Path(__file__).read_text()) or p["source"] != digest(sources())
            or p["settings_hash"] != digest(settings.public()) or p["manifest"] != digest(read(root / "dataset/manifest.json"))
            or p["calibration"] != digest(read(root / "calibration.json")) or p["oracle"] != digest(ORACLE)):
        raise ValueError("changed frozen protocol, code, data or settings")
    ledger = Store(settings.state_dir, unit=settings.accounting_unit)
    with (ledger.root / "comparison.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        st = Store(root / "state", ledger=ledger)
        exp = Study(root, settings, st, ledger, p)
        if (root / "result.json").is_file():
            finished = read(root / "result.json")
            if finished["protocol"] != exp.protocol:
                raise ValueError("wrong experiment result")
            return finished
        start = root / "start.json"
        if not start.is_file():
            with ledger.connect() as db:
                cursor = db.execute("SELECT coalesce(max(rowid),0) FROM calls").fetchone()[0]
            dump(start, {"protocol": exp.protocol, "cursor": cursor, "before": ledger.budget()})
        elif read(start)["protocol"] != exp.protocol:
            raise ValueError("wrong run registration")
        result = {"protocol": exp.protocol, "phase": "learning", "holdout": [], "curve": [], "published": False}
        try:
            study(exp, p, result)
            result["completed"] = True
        finally:
            with ledger.connect() as db:
                ids = [r[0] for r in db.execute("SELECT DISTINCT run_id FROM calls WHERE rowid>?", (read(start)["cursor"],))]
            # A provider-level reservation with unknown usage stays in the original shared ledger.
            allowed = {slot.stem for slot in (root / "slots").glob("*.json")}
            with st.connect() as db:
                allowed.update(r[0] for r in db.execute("SELECT DISTINCT run_id FROM events WHERE run_id LIKE 'proposal-%'"))
            result["costs"] = ledger.costs(sorted(set(ids) & allowed))
            result["unassigned_billing_run_count"] = len(set(ids) - allowed)
            if result["unassigned_billing_run_count"]:
                result["completed"] = False
            dump(root / "progress.json", result)
        if result.get("completed"):
            dump(root / "result.json", result)
        return result


def study(exp, p, result):
    st = exp.store
    def progress(phase):
        result["phase"] = phase
        dump(exp.root / "progress.json", result)
        print(phase, flush=True)

    def learn(key, pool=None):
        return exp.solve(exp.tasks[key], "learning", pool=pool, learning=True)

    first = bounded_map(learn, p["learning_seed"], p["workers"])
    result["unassisted_learning"] = first
    progress("root_proposal")
    parent = exp.proposal("root-proposal", first)
    result["parent"] = parent
    if parent is None:
        progress("proposal_rejected")
        result["anchor_unassisted"] = [st.run(exp.solve(exp.tasks[t], "gate"))["result"]["passed"] for t in ANCHORS]
        result["oracle_anchor_pass"] = exp.oracle()
        return

    progress("learning_application")
    later = bounded_map(lambda k: learn(k, [parent]), p["learning_trials"], p["workers"])
    engine = Evolution(exp.settings, st, feedback_policy="learn_outcome")
    eligible = []
    for key in later:
        run = st.run(key)
        feedback = engine.revision_feedback(run, st.events(key))
        if run["skill_id"] == parent and engine.qualifies({"execution_feedback": feedback}):
            eligible.append(key)
    result.update(application_runs=later, qualifying_failures=eligible)
    progress("candidate_freeze")
    child = exp.proposal("child-proposal", [first[0], *eligible[:4]], parent) if eligible else None
    candidate = child or parent
    result.update(child=child, candidate=candidate)
    lib = StudyLibrary(exp)
    dev = [t for t in exp.tasks.values() if t.split == "dev"]
    stop = threading.Event()

    def paired(job, old):
        t, rep, i = job
        return lib.compare_pair(t, rep, old, [candidate], candidate, reverse=bool((rep + i) % 2), stop=stop)
    progress("first_development_round")
    screen = bounded_map(lambda j: paired(j, []), [(t, 0, i) for i, t in enumerate(dev)], p["workers"])
    result.update(screen=screen, screen_gate=first_round_gate(screen))
    if child:
        anchors = [exp.tasks[k] for k in p["anchors"]]
        jobs = [(t, rep, i) for rep in range(2) for i, t in enumerate(anchors)]
        result["parent_child"] = bounded_map(lambda j: paired(j, [parent]), jobs, p["workers"])
    if not result["screen_gate"]["still_possible"]:
        progress("diagnostic_oracle")
        result["oracle_anchor_pass"] = exp.oracle()
        result["phase"] = "screen_rejected"
        return

    progress("full_gate")
    provenance = lib.provenance(candidate)
    gate_file = exp.root / "full-gate.json"
    if gate_file.exists():
        full = read(gate_file)
        if full["candidate"] != candidate or st.get_artifact(full["receipt"]) != {
                k: v for k, v in full.items() if k != "receipt"}:
            raise ValueError("saved gate receipt does not match candidate")
    else:
        full = lib.evaluate(candidate, dev,
                            [exp.tasks[i] for i in sorted(provenance["support"])], workers=p["workers"])
        dump(gate_file, full)
    result["gate"] = full
    if not full["gate"]["passed"]:
        result["phase"] = "gate_rejected"
        return
    if st.active_library() is None:
        st.activate_library(full["library_id"], full["receipt"], expected=None, mode="live")
    if st.active_library() != full["library_id"]:
        raise ValueError("unexpected active library")
    result["published"] = True
    progress("frozen_holdout")
    manifest = read(exp.root / "dataset/manifest.json")
    for family in p["family_order"]:
        tasks = [exp.tasks[row["id"]] for row in manifest["tasks"] if row["split"] == "test" and row["family"] == family]
        for task in tasks:
            exp.ledger.claim_test(task.id, digest(task.model_dump()), exp.protocol)
        pairs = bounded_map(lambda j: paired(j, []), [(t, 0, i) for i, t in enumerate(tasks)], p["workers"])
        result["holdout"].extend(dict(row, family=family) for row in pairs)
        result["curve"].append(curve_point(result["holdout"], family, pairs))
        progress("frozen_holdout")
    result["phase"] = "complete"


def first_round_gate(rows):
    if not rows or any(r["repeat"] != 0 or r["split"] != "dev" for r in rows):
        raise ValueError("feasibility requires the first development round")
    observed = gate_summary(rows, 1)
    return {"eligible_groups": observed["repeat_confirmed_improved_groups"], "regressions": observed["regressions"],
            "infrastructure_errors": observed["infrastructure_errors"], "still_possible": observed["gate"]["passed"]}


def curve_point(cumulative, family, pairs):
    def gain(rows):
        return sum(int(r["candidate_pass"]) - int(r["baseline_pass"]) for r in rows) / len(rows)
    families = Counter(r["family"] for r in cumulative)
    if len(set(families.values())) != 1:
        raise ValueError("cumulative diversity points require equally sized family strata")
    return {"families": len(families), "added_family": family, "pairs": len(cumulative),
            "family_gain": gain(pairs), "paired_gain": gain(cumulative)}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["prepare", "run"])
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--env-file", type=Path, default=Path(".env.glm"))
    args = parser.parse_args()
    cfg = Settings.from_env_file(args.env_file)
    cfg.validate_live()
    if cfg.mode != "live" or cfg.provider != "glm" or cfg.skill_routing != "semantic" or cfg.max_steps != 16:
        raise ValueError("this preregistered study requires live GLM, semantic routing and 16 total steps")
    if args.command == "prepare":
        print("frozen", digest(prepare(args.root, cfg)))
    else:
        report = execute(args.root, cfg)
        print(report["phase"], report["costs"], flush=True)
