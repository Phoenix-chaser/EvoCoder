"""Learn a procedural revision on independent tasks and evaluate under the original normal-routing gate."""
import argparse
import ast
import fcntl
import json
import time
from pathlib import Path
from threading import Event

from continue_route_study import ObserveRateLimit
from evocoder.comparison import dump, frozen_knowledge, sources
from evocoder.contracts import Settings, Task, digest, safe_path
from evocoder.dataset import load_catalog
from evocoder.errors import ProposalRejected
from evocoder.evolution import Evolution
from evocoder.gate import gate_summary
from evocoder.library import SkillLibrary
from evocoder.model import Model
from evocoder.revision_diagnostic import check_fresh_holdouts, copy_closed_seed, freeze_protocol, read, summarize_feedback
from evocoder.route_study import CASES
from evocoder.runtime import Runner
from evocoder.store import Store

CLAIM = "independent_learn_procedure_revision"
MISSING = "missing_cli_state_revision"
TERMINAL = {"COMPLETE", "FAILED", "EXHAUSTED"}


def pick(manifest, ids):
    entries = {e["id"]: e for e in read(manifest)["tasks"]}
    tasks = {}
    for key in ids:
        entry = entries[key]
        task = Task.model_validate(read(manifest.parent / safe_path(entry["path"])))
        if (task.id != key or digest(task.model_dump()) != entry["hash"]
                or (task.split, task.source_group) != (entry["split"], entry["source_group"])):
            raise ValueError(f"task snapshot changed: {key}")
        tasks[key] = task
    return tasks


def options_for(settings, archived, missing=False):
    extra = {"public_checkpoints": True, **({"authored_cli_checks": True} if missing else {})}
    opts = settings.model_copy(update={"image": archived["image"], **extra})
    opts.validate_live()
    if opts.mode != "live" or opts.provider != "glm" or opts.public() != {**archived, **extra}:
        raise ValueError("expected the original GLM solver configuration plus public checkpoints")
    return opts


def check_learning(learn, manifest, image, history, missing=False):
    data, proof = read(manifest), read(manifest.parent / "verification.json")
    if (len(learn) < 2 or len(learn) > 3 or any(t.split != "learn" for t in learn)
            or len({t.source_group for t in learn}) != len(learn)
            or proof["manifest_hash"] != digest(data) or proof["model_calls"] != 0
            or not proof["passed"] or proof["image_id"] != image):
        raise ValueError("missing independently verified learning tasks")
    rows = {r["id"]: r for r in proof["rows"]}
    if len(rows) != len(learn) or len(proof["rows"]) != len(rows):
        raise ValueError("incomplete learning proof")
    for t in learn:
        r = rows[t.id]
        if missing:
            from prepare_click_procedure import REPRODUCTION
            nodes = [node.name for node in ast.walk(ast.parse(t.files[REPRODUCTION]))
                     if isinstance(node, ast.FunctionDef) and node.name.startswith("test_")]
            if (nodes != ["test_feature_enabled"] or data["public_nodes"] != [REPRODUCTION + "::test_feature_enabled"]
                    or t.public_command[-1] != REPRODUCTION or r["checks"]["reference_public"]["junit"]["tests"] != 1):
                raise ValueError("learning task must provide exactly one enabled public example")
        if not (r["passed"] and r["task_hash"] == digest(t.model_dump()) and r["image_id"] == image):
            raise ValueError("unbound learning verification")
        for k, v in {"buggy_public": 1, "buggy_regression": 1, "reference_public": 0,
                     "reference_regression": 0}.items():
            check = r["checks"][k]
            allowed = {0, 1} if missing and k == "buggy_public" else {v}
            if check["exit_code"] not in allowed or check["junit"]["errors"] or check["image_id"] != image:
                raise ValueError(f"unexpected verification outcome {t.id}/{k}")
    # Compare metadata only; do not open unrelated task bodies or independent test contents.
    previous = []
    for path in sorted(history.rglob("manifest.json")):
        if path.resolve() == manifest:
            continue
        other = read(path)
        if isinstance(other, dict):
            previous.extend(e for e in other.get("tasks", []) if isinstance(e, dict))
    for field, values in (("id", {t.id for t in learn}), ("hash", {digest(t.model_dump()) for t in learn}),
                          ("source_group", {t.source_group for t in learn})):
        if any(e.get(field) in values for e in previous):
            raise ValueError(f"learning {field} overlaps historical manifest")
    return proof


def driver_sources(missing):
    names = ["continue_route_study.py"]
    if missing:
        names += ["prepare_click_missing_state.py", "verify_authored_cli_checks.py", "verify_step_admission.py",
                  "audit_click_step_claim.py", "probe_click_route_patches.py", "verify_cli_reach.py",
                  "compare_click_fixed_steps.py"]
    paths = [Path(__file__), *(Path(__file__).with_name(n) for n in names)]
    return {path.name: path.read_text() for path in paths}


def prepare(settings, origin, manifest, root, missing=False):
    origin, manifest, root = origin.resolve(), manifest.resolve(), root.resolve()
    p, result = read(origin / "protocol.json"), read(origin / "result.json")
    previous_claim = CLAIM if missing else "routing_intervention_diagnostic"
    blocked = "blocked_by_focus" if missing else "blocked_by_route_gate"
    if (p["claim"] != previous_claim or not result["completed"] or result["protocol"] != p
            or result["holdout"]["status"] != blocked or result["holdout"]["rows"]):
        raise ValueError("expected closed prior route study with fresh holdouts")
    eval_path = Path(p["manifest"])
    if digest(read(eval_path)) != p["manifest_hash"]:
        raise ValueError("changed development data")
    opts = options_for(settings, p["settings"], missing)
    ledger = Store(opts.state_dir, unit=opts.accounting_unit)
    if ledger.budget()["blocked"]:
        raise ValueError("shared billing ledger blocked")
    learn = load_catalog(manifest)
    proof = check_learning(learn, manifest, opts.image, origin.parent, missing)
    independence = read(manifest.parent / "independence.json")
    if (independence["manifest_hash"] != digest(read(manifest)) or independence["model_calls"] != 0
            or not independence["passed"] or not independence["historical_manifest_hashes"]):
        raise ValueError("missing historical learning independence proof")
    check_fresh_holdouts(learn, origin.parent, ledger, label="new learning case")
    extra = {}
    if missing:
        from verify_authored_cli_checks import calibrate
        from verify_step_admission import verify
        admission = verify(origin)
        controls = calibrate(manifest, eval_path, opts.image)
        support_manifest = Path(p["learn_manifest"])
        if digest(read(support_manifest)) != p["learn_hash"]:
            raise ValueError("changed predecessor learning tasks")
        extra = {"controls_hash": digest(controls), "admission_hash": digest(admission),
                 "support_manifests": {str(support_manifest): p["learn_hash"]}}
    st = copy_closed_seed(origin, root, ledger)
    if missing:
        dump(root / "step-admission.json", admission)
        dump(root / "cli-controls.json", controls)
    parent = result["candidate"] if missing else p["child"]
    provenance = SkillLibrary(opts, st).provenance(parent)
    if (p["parent"] not in provenance["members"] or (not missing and provenance["root"] != p["parent"])
            or st.active_library() or st.active()):
        raise ValueError("unexpected skill ancestry or released version")
    with ledger.connect() as db:
        last_bill = db.execute("SELECT coalesce(max(rowid),0) FROM calls").fetchone()[0]
    external = driver_sources(missing)
    dump(root / "drivers.json", external)
    protocol = freeze_protocol(root, {
        **extra, "claim": MISSING if missing else CLAIM, "origin": str(origin), "origin_hash": digest(result), "parent": parent,
        "settings": opts.public(), "drivers_hash": digest(external), "learn_manifest": str(manifest),
        "learn_hash": digest(read(manifest)), "learn_verification_hash": digest(proof),
        "independence_hash": digest(independence),
        "manifest": str(eval_path), "manifest_hash": p["manifest_hash"], "learn_ids": [t.id for t in learn],
        "focus": list(CASES), "dev": p["dev"] if missing else p["prior_dev_tasks"],
        "sentinels": p["sentinels"] if missing else p["regression_tasks"],
        "holdouts": p["holdouts"] if missing else p["test_tasks"], "prior_support": sorted(provenance["support"]),
        "budget_before": ledger.budget(), "previous_billing_row": last_bill,
        "policy": {"learning": "apply parent for one wave; repeat entire cohort once only without a qualifying terminal failure; prefer terminal failures and fall back to observed recovered post-edit public failures only if no terminal failures in either wave",
                   "proposal": "only actual failed parent applications from these new learning cases; at most two attempts on identical evidence, second only for settled structural rejection",
                   "diagnostic": "one fixed child replay per new learning case against its first fixed parent trial",
                   "focus": "normal route, each focus case twice with reversed order on second repeat",
                   "gate": "require two independent dev groups with two skill-related wins each, zero regressions and zero infrastructure errors on scored pairs; normal support and known regression replay before holdout",
                   "holdout": "two normal pairs per locally fresh task only after full gate",
                   "publication": False},
    })
    if missing:
        # Register both assessments before the first call in a new study.
        from compare_click_fixed_steps import register as register_fixed
        from verify_cli_reach import register as register_reach
        register_fixed(root)
        register_reach(root)
    return protocol


def gate_still_possible(rows, remaining_groups):
    summary = gate_summary(rows, 2)
    eligible = (not summary["regressions"] and not summary["infrastructure_errors"]
                and len(summary["repeat_confirmed_improved_groups"]) + remaining_groups >= 2)
    return eligible, summary


class TrialRunner:
    """Persist individual arms. A transient provider error never becomes a scored outcome."""
    def __init__(self, opts, store, root, protocol):
        self.store, self.root, self.p = store, root, protocol
        self.transport = ObserveRateLimit()
        self.model = Model(opts, store, self.transport)
        self.runner = Runner(opts, store, self.model)
        self.slots = read(root / "slots.json") if (root / "slots.json").exists() else {}
        self.phase = ""
        self.repeat = 0

    def solve(self, task, **kwargs):
        skill, = kwargs["skill_ids"]
        slot = f"{self.phase}/{task.id}/{self.repeat}/{skill}"
        attempts = self.slots.setdefault(slot, [])
        for index in range(3):
            run_id = digest([digest(self.p), slot, index])[:32]
            if index == len(attempts):
                attempts.append({"run_id": run_id})
                dump(self.root / "slots.json", self.slots)
            record = attempts[index]
            if record["run_id"] != run_id:
                raise ValueError("changed trial slot")
            with self.store.connect() as db:
                exists = db.execute("SELECT 1 FROM runs WHERE id=?", (run_id,)).fetchone()
            if not exists:
                self.transport.last = None
                self.runner.solve(task, run_id=run_id, **kwargs)
                if self.transport.last:
                    record["rate_limit"] = self.transport.last
                dump(self.root / "slots.json", self.slots)
            run = self.store.run(run_id)
            start = next(e["payload"] for e in self.store.events(run_id) if e["kind"] == "START")
            if (run["task_hash"] != digest(task.model_dump()) or run["config"] != self.p["settings"]
                    or run["purpose"] != kwargs.get("purpose", "solve") or start["requested_skill_ids"] != [skill]
                    or start.get("fixed_route") != kwargs.get("fixed_route")):
                raise ValueError("trial does not match frozen execution")
            if run["state"] in TERMINAL:
                if run["result"]["image_id"] != self.p["settings"]["image"]:
                    raise ValueError("solver ran with different image")
                return run_id
            rate = record.get("rate_limit") or {}
            if (run["state"] != "ERROR" or "model_http_429" not in str(run["result"])
                    or rate.get("code") not in {1302, 1305} or index == 2):
                raise RuntimeError(f"unscored {run['state']} in {slot} ({run_id}): {(run['result'] or {}).get('error')}")
            delay = max(rate.get("retry_after_seconds") or 0, (60, 180)[index])
            while (left := rate["observed_at"] + delay - time.time()) > 0:
                time.sleep(min(left, 30))


def compare(lib, runner, task, rep, phase, reverse, parent, child):
    runner.phase, runner.repeat = phase, rep
    row = lib.compare_pair(task, rep, [parent], [child], child, reverse=reverse, stop=Event())
    row.update(policy="normal", arm_order=["candidate", "baseline"] if reverse else ["baseline", "candidate"])
    return row


def study(opts, store, root, p, r):
    from evocoder.procedure_evidence import behavior_receipt

    learning = pick(Path(p["learn_manifest"]), p["learn_ids"])
    runner = TrialRunner(opts, store, root, p)
    lib = SkillLibrary(opts, store, runner)
    parent = p["parent"]
    policy = "bound_procedure" if p["claim"] == MISSING else "learn_procedure_feedback"
    evo = Evolution(opts, store, model=runner.model, feedback_policy="learn_outcome",
                    proposal_policy=policy, grounding_tasks=learning)

    def progress():
        dump(root / "progress.json", r)

    def observe(task, key):
        receipt = behavior_receipt(store, task, key)
        dump(root / "behavior" / f"{key}.json", receipt)
        return digest(receipt)

    terminal_failures, recovered_failures = [], []
    for wave in range(2):
        runner.phase, runner.repeat = "learning", wave
        for task in learning.values():
            key = runner.solve(task, use_active=False, candidate=True, skill_ids=[parent], fixed_route="apply")
            trial = summarize_feedback(store, evo, store.run(key), parent)
            if trial["applied_skill"] != parent:
                raise ValueError("parent learning trial missing actual injection")
            trial.update(repeat=wave, behavior_hash=observe(task, key))
            r["learning"].append(trial)
            if trial["eligible_for_revision"]:
                if trial["state"] in {"FAILED", "EXHAUSTED"} and not trial["passed"]:
                    terminal_failures.append(key)
                elif trial["execution_feedback"]["post_edit_public_failure_sequences"]:
                    recovered_failures.append(key)
            progress()
        if terminal_failures:
            break
    eligible = terminal_failures or recovered_failures
    r["evidence_kind"] = "terminal_failure" if terminal_failures else "recovered_public_failure" if recovered_failures else "none"
    if not eligible:
        r.update(outcome="no_qualifying_parent_failure")
        return
    r.update(phase="proposal", eligible_runs=eligible)
    progress()
    # Recover a registered proposal by its learning evidence. Never rerun a finished proposal for a better answer.
    evhash, _ = evo.freeze(eligible, parent)
    with store.connect() as db:
        previous = [(row[0], json.loads(row[1])) for row in db.execute(
            "SELECT run_id,payload FROM events WHERE kind='PROPOSAL_START' ORDER BY rowid")
            if json.loads(row[1]).get("evidence_hash") == evhash and json.loads(row[1]).get("parent_id") == parent]
    candidate = None
    for key, detail in previous:
        if detail["proposal_policy"] != policy:
            raise ValueError("previous proposal policy differs")
        events = store.events(key)
        accepted = [e["payload"]["skill_id"] for e in events if e["kind"] == "PROPOSED"]
        if accepted:
            candidate, = accepted
        elif not any(e["kind"] == "PROPOSAL_ERROR" for e in events) or store.costs([key])["reserved_credits"]:
            raise ValueError(f"unresolved proposal {key}")
    for index in range(len(previous), 2):
        if candidate:
            break
        try:
            candidate = evo.propose(eligible, parent)
        except ProposalRejected as exc:
            rejected = r.setdefault("proposal_rejections", [])
            rejected.append(str(exc))
            progress()
    if not candidate:
        r.update(outcome="proposal_rejected")
        return
    child = store.get_artifact(candidate)
    if (child["parent"] != parent or set(child["evidence_refs"]) - set(eligible)
            or child["steps"] == store.get_artifact(parent)["steps"]):
        raise ValueError("candidate lacks learn-only procedural revision")
    ancestry = lib.provenance(candidate)
    baseline_knowledge = frozen_knowledge(store)
    r.update(candidate=candidate, phase="learning_replay", knowledge_hash=baseline_knowledge)
    dump(root / "revision.json", {"parent_id": parent, "candidate_id": candidate, "parent": store.get_artifact(parent),
                                 "candidate": child, "eligible_ids": eligible, "frozen_evidence": evhash})
    progress()
    runner.phase, runner.repeat = "learning_replay", 0
    for task in learning.values():
        key = runner.solve(task, use_active=False, purpose="evaluate", skill_ids=[candidate], fixed_route="apply")
        trial = store.run(key)
        r["learning_replay"].append({"task_id": task.id, "run_id": key, "passed": trial["result"]["passed"],
                                     "state": trial["state"], "behavior_hash": observe(task, key)})
        progress()
    evaluation = Path(p["manifest"])
    support = sorted(ancestry["support"])
    if not set(p["prior_support"]) <= set(support) or not set(support) <= set(p["prior_support"]) | set(learning):
        raise ValueError("invalid support provenance")
    prior_tasks = {}
    for name, expected in p.get("support_manifests", {}).items():
        path = Path(name)
        m = read(path)
        if digest(m) != expected:
            raise ValueError("changed historical support manifest")
        prior_tasks.update(pick(path, [x["id"] for x in m["tasks"] if x["id"] in p["prior_support"]]))
    needed = set(p["prior_support"]) - prior_tasks.keys()
    tasks = {**pick(evaluation, [*p["dev"], *p["sentinels"], *needed]), **prior_tasks, **learning}
    if any(digest(tasks[k].model_dump()) != ancestry["support"][k] for k in support):
        raise ValueError("changed support snapshot")
    paired_gate(p, r, root, store, tasks, support, lib, runner, parent, candidate, observe)


def paired_gate(p, r, root, store, tasks, support, lib, runner, parent, candidate, observe):
    def progress():
        dump(root / "progress.json", r)

    r["phase"] = "focus"
    for rep in range(2):
        for idx, key in enumerate(p["focus"]):
            row = compare(lib, runner, tasks[key], rep, "focus", bool((rep+idx) % 2), parent, candidate)
            for arm in ("baseline", "candidate"):
                row[arm + "_behavior_hash"] = observe(tasks[key], row[arm + "_run"])
            r["rows"].append(row)
            progress()
    remaining = [key for key in p["dev"] if key not in p["focus"]]
    possible, r["focus_gate"] = gate_still_possible(r["rows"], len({tasks[k].source_group for k in remaining}))
    if not possible:
        r["gate"]["status"] = "blocked_by_focus"
        r["holdout"]["status"] = "blocked_by_focus"
        r["outcome"] = "focus_gate_failed"
        return
    r["phase"] = "full_gate"
    full = r["gate"]
    full["rows"] = r["rows"].copy()
    for role, keys, repeats in (("other_dev", remaining, 2), ("known_regression", p["sentinels"], 2),
                               ("support", support, 1)):
        for rep in range(repeats):
            for idx, key in enumerate(keys):
                row = compare(lib, runner, tasks[key], rep, role, bool((rep+idx) % 2), parent, candidate)
                row["evaluation_role"] = role
                full["rows"].append(row)
                progress()
    full.update(status="complete", **gate_summary(full["rows"], 2))
    if not full["gate"]["passed"]:
        r["holdout"]["status"] = "blocked_by_full_gate"
        r["outcome"] = "full_gate_failed"
        return
    r["phase"] = "holdout"
    hold = pick(Path(p["manifest"]), p["holdouts"])
    exposure = root / "holdout-start.json"
    if not exposure.exists():
        check_fresh_holdouts(list(hold.values()), Path(p["origin"]).parent, store.ledger)
        dump(exposure, {"protocol": digest(p), "tasks": {k: digest(t.model_dump()) for k, t in hold.items()}})
    if read(exposure) != {"protocol": digest(p), "tasks": {k: digest(t.model_dump()) for k, t in hold.items()}}:
        raise ValueError("holdout exposure mismatch")
    for rep in range(2):
        for idx, task in enumerate(hold.values()):
            store.claim_test(task.id, digest(task.model_dump()), digest(p))
            row = compare(lib, runner, task, rep, "holdout", bool((rep+idx) % 2), parent, candidate)
            r["holdout"]["rows"].append(row)
            progress()
    r["holdout"]["status"] = "complete"
    r["outcome"] = "holdout_evaluated"


def execute(settings, root):
    root = root.resolve()
    p = read(root / "protocol.json")
    origin = Path(p["origin"])
    missing = p["claim"] == MISSING
    opts = options_for(settings, read(origin / "protocol.json")["settings"], missing)
    drivers = driver_sources(missing)
    if (p["claim"] not in {CLAIM, MISSING} or opts.public() != p["settings"] or digest(sources()) != p["source_hash"]
            or digest(read(root / "drivers.json")) != digest(drivers) or digest(drivers) != p["drivers_hash"]
            or digest(read(origin / "result.json")) != p["origin_hash"]
            or digest(read(Path(p["manifest"]))) != p["manifest_hash"]
            or digest(read(Path(p["learn_manifest"]))) != p["learn_hash"]
            or digest(read(Path(p["learn_manifest"]).parent / "verification.json")) != p["learn_verification_hash"]
            or digest(read(Path(p["learn_manifest"]).parent / "independence.json")) != p["independence_hash"]):
        raise ValueError("changed frozen setup")
    if missing and (digest(read(root / "step-admission.json")) != p["admission_hash"]
                    or digest(read(root / "cli-controls.json")) != p["controls_hash"]):
        raise ValueError("changed pre-dispatch evidence controls")
    ledger = Store(opts.state_dir, unit=opts.accounting_unit)
    with (ledger.root / "comparison.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        store = Store(root / "seed", ledger=ledger)
        if (root / "result.json").exists() and read(root / "result.json")["completed"]:
            raise ValueError("this experiment has completed")
        if not (root / "started.json").exists():
            if ledger.budget() != p["budget_before"]:
                raise ValueError("ledger changed before dispatch")
            dump(root / "started.json", {"protocol_hash": digest(p)})
        if read(root / "started.json") != {"protocol_hash": digest(p)}:
            raise ValueError("unbound start receipt")
        r = {"protocol": p, "completed": False, "phase": "learning", "candidate": None, "learning": [],
             "learning_replay": [], "rows": [], "gate": {"status": "pending", "rows": []},
             "holdout": {"status": "not_run", "rows": []}}
        try:
            study(opts, store, root, p, r)
            if r.get("candidate") and frozen_knowledge(store) != r["knowledge_hash"]:
                raise ValueError("skills changed during evaluation")
            r.update(completed=True, phase="complete")
        except Exception as exc:
            r["error"] = f"{type(exc).__name__}: {exc}".replace(opts.api_key, "[redacted]")[:1300]
        finally:
            with ledger.connect() as db:
                ids = [x[0] for x in db.execute("SELECT DISTINCT run_id FROM calls WHERE rowid>? ORDER BY rowid",
                                                 (p["previous_billing_row"],))]
            with store.connect() as db:
                local = {x[0] for x in db.execute("SELECT DISTINCT run_id FROM events")}
                r["run_ids"] = [k for k in ids if db.execute("SELECT 1 FROM runs WHERE id=?", (k,)).fetchone()]
            if set(ids) - local:
                r.update(completed=False, error="shared ledger contains unrelated concurrent calls")
            r.update(billed_ids=ids, costs=store.costs(ids), budget_after=ledger.budget(), published=store.active_library())
            delta = r["budget_after"]["spent_credits"] - p["budget_before"]["spent_credits"]
            if abs(delta - r["costs"]["cost_credits"]) > 1e-6:
                r.update(completed=False, error="ledger spent delta disagrees with requests")
            dump(root / "result.json", r)
        return r


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["prepare", "run"])
    parser.add_argument("--missing-states", action="store_true")
    parser.add_argument("--origin", type=Path, default=Path("experiments/glm-click-routing-matrix-002"))
    parser.add_argument("--learn", type=Path, default=Path("experiments/click-procedure-learn-001/manifest.json"))
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--env-file", type=Path, default=Path(".env.glm"))
    args = parser.parse_args()
    settings = Settings.from_env_file(args.env_file)
    if args.command == "prepare":
        protocol = prepare(settings, args.origin, args.learn, args.root, args.missing_states)
        print(json.dumps({k: protocol[k] for k in ("learn_ids", "parent", "focus")}))
    else:
        result = execute(settings, args.root)
        print(json.dumps({k: result.get(k) for k in ("completed", "phase", "outcome", "candidate", "costs", "error")}))
        raise SystemExit(0 if result["completed"] else 1)
