"""Learn one child from separate object tasks, then compare it to the frozen parent and deployed baseline."""

import argparse
import copy
import fcntl
import threading
from pathlib import Path

from evocoder.comparison import clone_state, dump
from evocoder.contracts import Settings, canonical, digest
from evocoder.dataset import load_catalog
from evocoder.errors import ProposalRejected
from evocoder.evolution import Evolution
from evocoder.gate import gate_summary
from evocoder.jobs import bounded_map
from evocoder.store import Store
from falsy_transfer_protocol import FAMILIES, LEARNED, REFERENCE, probe_patch
from object_revision_cases import cases
from report_click_procedure import Archive, read
from run_falsy_objects import extra_hashes
from run_falsy_transfer import TransferStudy, atomic_trial, require
from run_optional_control import StudyLibrary, curve_point

HERE = Path(__file__).resolve().parent
PUBLIC = Path("benchmarks/falsy_transfer/object-revision.json")
OLD = Path("docs/results")
OBJECT_ARMS = ("bare", "parent", "child", "fixed_parent", "fixed_child", "manual")
CONFIG_ARMS = ("bare", "parent", "child")
POLICY = "grounded_scope"
WORKERS = 3


def sources():
    return extra_hashes() | {
        n: digest((HERE / n).read_text()) for n in ("object_revision_cases.py", "run_object_revision.py")
    }


def order(arms, index, repeat):
    i = index % len(arms)
    seq = list(arms[i:] + arms[:i])
    return list(reversed(seq)) if repeat % 2 else seq


def prepare(root, panel, seed, cfg):
    require(not (root / "protocol.json").exists(), "protocol already registered; use run to resume")
    old = read(panel / "protocol.json")
    prior = read(panel / "result.json")
    first = read(OLD / "falsy-transfer.json")
    public = read(OLD / "falsy-objects.json")
    require(
        prior["completed"]
        and prior["phase"] == public["phase"] == "object_gate_rejected"
        and prior["protocol"] == public["protocol"] == digest(old)
        and prior["gate"]["receipt"] == public["gate"]["receipt"]
        and not prior["published"]
        and prior["holdout_exposures"] == 0
        and first["protocol"] == old["primary"]
        and old["settings_hash"] == digest(cfg.public())
        and old["learned"] == LEARNED
        and old["manual_hash"] == REFERENCE.hash,
        "changed historical comparison",
    )
    require(read(seed / "root-proposal.json")["skill"] == LEARNED, "unexpected learned root")
    require(digest(read(panel / "dataset/manifest.json")) == old["manifest_hash"], "old panel differs")
    previous = {t.id: t for t in load_catalog(panel / "dataset/manifest.json")}
    support = first["support"]
    ids = [*support, *old["dev"], *old["previous_dev"], *(t for ts in old["holdout"].values() for t in ts)]
    authored = cases()

    def calibration(item):
        t, ref = item
        broken, fixed = (probe_patch(t, patch, cfg) for patch in ({}, ref))
        require(
            not broken["public"]
            and broken["controls"]
            and all(fixed.values())
            and not any(v for k, v in broken.items() if k not in {"public", "controls"}),
            "invalid calibration",
        )
        return {
            "id": t.id,
            "snapshot": digest(t.model_dump()),
            "reference": digest(ref),
            "broken": broken,
            "fixed": fixed,
        }

    cal = bounded_map(calibration, authored, WORKERS)
    require(
        len({t.source_group for t, _ in authored}) == len(authored)
        and not {t.source_group for t, _ in authored} & {t.source_group for t in previous.values()},
        "new learning overlaps earlier tasks",
    )
    directory = root / "dataset"
    directory.mkdir(parents=True, exist_ok=True)
    entries = []
    for t in [*(t for t, _ in authored), *(previous[k] for k in ids)]:
        path = t.id + ".json"
        (directory / path).write_text(canonical(t.model_dump()) + "\n")
        entries.append(
            {
                "id": t.id,
                "path": path,
                "hash": digest(t.model_dump()),
                "source_group": t.source_group,
                "split": t.split,
            }
        )
    dump(directory / "manifest.json", {"tasks": entries})
    load_catalog(directory / "manifest.json")
    protocol = {
        "study": "caller-object-skill-revision-v1",
        "codes": sources(),
        "model": cfg.model,
        "image": cfg.image,
        "settings": digest(cfg.public()),
        "parent": LEARNED,
        "manual": REFERENCE.hash,
        "old_panel": digest(old),
        "old_gate": prior["gate"]["receipt"],
        "published_observations": {"falsy-transfer": digest(first), "falsy-objects": digest(public)},
        "tasks": entries,
        "calibration": cal,
        "learn": [t.id for t, _ in authored],
        "objects": old["dev"],
        "config": old["previous_dev"],
        "root_support": support,
        "holdout": old["holdout"],
        "object_arms": list(OBJECT_ARMS),
        "config_arms": list(CONFIG_ARMS),
        "repeats": 2,
        "solver_slots": 15,
        "workers": WORKERS,
        "proposal_policy": POLICY,
        "learning": "Once normally routed and twice with the parent explicitly applied on each new learn task. "
        "Only terminal failed/exhausted actual parent applications with qualifying execution feedback "
        "can trigger revision. Choose up to three different tasks, submitted failures first. Add up to "
        "one same-task natural skip and one successful parent application for contrast.",
        "proposal": "Use the parent, selected learn episodes and verified patch/test receipts. The proposer sees "
        "no development, holdout, manual text, reference fixes or hidden probes. Stop at the first "
        "accepted child. One identical-evidence retry only for a settled invalid proposal.",
        "comparison": "Three object tasks from the previously observed panel, four original configuration dev tasks, "
        "two reversed-order repeats. New runs for all arms. Controlled and manual arms are descriptive. "
        "Near manual means at least 5/6 and within one repair of the concurrent manual arm, descriptively.",
        "release": "Require normal child vs normal parent to pass the standard repeated-win gate on object tasks "
        "and on the complete development/support comparison. Require the normal child vs no skill to "
        "pass the standard full-library gate on all seven tasks and all ancestral support. Retain every "
        "observed loss. No adjustment after inspecting development. A historical 2/6 is forced-parent only.",
        "test": "After actual library activation only, evaluate the original 30 once in matched no-skill/normal-child "
        "pairs with no learning. Scalar, layered and batch are configuration strata. The object panel is "
        "a revisited development panel, not an unseen caller-object generalization test.",
    }
    dump(root / "protocol.json", protocol)
    return protocol


def arm_run(study, task, phase, repeat, arm, child=None, *, learning=False):
    key = {
        "bare": None,
        "parent": LEARNED,
        "child": child,
        "fixed_parent": LEARNED,
        "fixed_child": child,
        "manual": REFERENCE.hash,
    }[arm]
    if arm.startswith("fixed_") or arm == "manual":
        forced = copy.copy(study)
        forced.oracle_store = study.manual if arm == "manual" else study.store
        forced.oracle_key = key
        require(
            key
            and len(canonical({"skills": [forced.oracle_store.get_artifact(key)]}).encode())
            <= study.settings.max_memory_bytes,
            "fixed arm would exceed the normal solver memory budget",
        )
        run_id = forced.solve(task, f"{phase}-{arm}", repeat, oracle=True, learning=learning)
        store = forced.oracle_store
    else:
        require(arm == "bare" or key, "unbound candidate")
        run_id = study.solve(task, phase, repeat, pool=[key] if key else None, learning=learning)
        store = study.store
    r = store.run(run_id)
    events = store.events(run_id)
    final = r["result"]
    (route,) = [e["payload"] for e in events if e["kind"] == "SKILL_ROUTE"]
    replies = [e["payload"] for e in events if e["kind"] == "MODEL_RESPONSE"]
    first = next(c for c in replies if c["expected_function"] == "submit_action")
    require(
        r["purpose"] == ("solve" if learning else "evaluate")
        and r["mode"] == "live"
        and r["task_hash"] == digest(task.model_dump())
        and final["image_id"] == study.settings.image
        and all(c["provider_model"].lower() == study.settings.model.lower() for c in replies)
        and r["skill_id"] == route["applied_skill_id"],
        "run/route/model mismatch",
    )
    require(
        (route["decision"] == "forced" and r["skill_id"] == key)
        if arm.startswith("fixed_") or arm == "manual"
        else r["skill_id"] in {None, key},
        "wrong arm",
    )
    steps = sum(e["kind"] == "ACTION" for e in events)
    require(steps <= 15, "solver exceeded registered budget")
    row = {
        "run": run_id,
        "state": r["state"],
        "passed": final["passed"],
        "route": route["decision"],
        "applied": r["skill_id"],
        "patch": final["patch"],
        "steps": steps,
        "calls": final["cost"]["calls"],
        "first_solver_request": first["request_body_hash"],
    }
    if learning:
        engine = Evolution(study.settings, store, feedback_policy="learn_outcome")
        fb = engine.revision_feedback(r, events)
        row.update(
            feedback=fb,
            eligible=(
                r["skill_id"] == LEARNED
                and not final["passed"]
                and engine.qualifies({"execution_feedback": fb})
            ),
        )
    if final["patch"] and any(k.startswith("probe_") for k in task.grader_files):
        probes = probe_patch(task, store.get_artifact(final["patch"]), study.settings)
        require(all(probes.values()) == final["passed"], "replayed patch disagrees with verdict")
        row["probes"] = probes
    return row


def learning_trials(s, p):
    def trial(job):
        i, tid, n = job
        sequence = order(("parent", "fixed_parent"), i, n) if n == 0 else ["fixed_parent"]
        row = {"task": tid, "repeat": n, "order": sequence}
        for arm in sequence:
            row[arm] = arm_run(s, s.tasks[tid], "learning", n, arm, learning=True)
        atomic_trial(s.root / "trials" / f"learn-{tid}-{n}.json", row)
        print(
            "learn", tid, n + 1, *(f"{a}={row[a]['state']}/{row[a]['route']}" for a in sequence), flush=True
        )
        return row

    return bounded_map(trial, [(i, t, n) for n in range(2) for i, t in enumerate(p["learn"])], p["workers"])


def select_evidence(rows):
    entries = [(r["task"], r[a]) for r in rows for a in r["order"]]
    eligible = [(t, a) for t, a in entries if a["eligible"]]
    # No task-specific probe name, oracle result or development outcome enters evidence selection.
    eligible.sort(
        key=lambda pair: (
            not bool(pair[1]["patch"]),
            not pair[1]["feedback"].get("learn_submission_failed_independent_check"),
            pair[1]["route"] == "forced",
        )
    )
    selected = {}
    for t, a in eligible:
        selected.setdefault(t, a["run"])
        if len(selected) == 3:
            break
    if not selected:
        return []
    result = list(selected.values())
    skipped = next((a["run"] for t, a in entries if t in selected and a["applied"] is None), None)
    success = next((a["run"] for _, a in entries if a["passed"] and a["applied"] == LEARNED), None)
    return result + [rid for rid in (skipped, success) if rid]


def propose(s, refs):
    engine = Evolution(
        s.settings,
        s.store,
        feedback_policy="learn_outcome",
        proposal_policy=POLICY,
        grounding_tasks={t.id: t for t in s.tasks.values() if t.split == "learn"},
    )
    evidence, _ = engine.freeze(refs, LEARNED)

    def proposals():
        with s.store.connect() as db:
            return [
                r[0]
                for r in db.execute(
                    "SELECT run_id FROM events WHERE kind='PROPOSAL_START' "
                    "AND json_extract(payload,'$.evidence_hash')=? AND json_extract(payload,'$.proposal_policy')=? "
                    "ORDER BY rowid",
                    (evidence, POLICY),
                )
            ]

    while True:
        attempts = proposals()
        for rid in attempts:
            events = s.store.events(rid)
            if events[-1]["kind"] == "PROPOSED":
                key = events[-1]["payload"]["skill_id"]
                bound = next(e["payload"]["artifact"] for e in events if e["kind"] == "SUMMARY_VERIFIED")
                receipt = s.store.get_artifact(bound)
                require(
                    receipt["candidate"] == key
                    and receipt["frozen_evidence"] == evidence
                    and receipt["parent"] == LEARNED,
                    "ungrounded child",
                )
                return {"child": key, "evidence": evidence, "receipt": bound, "attempts": attempts}
            require(
                events[-1]["kind"] == "PROPOSAL_ERROR"
                and s.store.costs([rid])["calls"] > 0
                and not s.store.costs([rid])["reserved_credits"]
                and not s.ledger.budget()["blocked"],
                "unfinished proposal; recover original request before issuing a new one",
            )
        if len(attempts) >= 2:
            return {"child": None, "evidence": evidence, "receipt": None, "attempts": attempts}
        try:
            engine.propose(refs, LEARNED)
        except ProposalRejected:
            continue


def development(s, p, child):
    def trial(item):
        i, tid, n = item
        arms = OBJECT_ARMS if tid in p["objects"] else CONFIG_ARMS
        seq = order(arms, i, n)
        row = {"task": tid, "repeat": n, "order": seq}
        for a in seq:
            row[a] = arm_run(s, s.tasks[tid], "gate", n, a, child)
        atomic_trial(s.root / "trials" / f"dev-{tid}-{n}.json", row)
        print("dev", tid, n + 1, *(f"{a}={int(row[a]['passed'])}/{row[a]['route']}" for a in seq), flush=True)
        return row

    jobs = [(i, t, n) for n in range(2) for group in (p["objects"], p["config"]) for i, t in enumerate(group)]
    return bounded_map(trial, jobs, p["workers"])


def release_decision(object_rows, parent, deployed):
    objects = gate_summary(object_rows, 2)
    return {
        "objects_vs_parent": objects,
        "passed": bool(objects["gate"]["passed"] and parent["gate"]["passed"] and deployed["gate"]["passed"]),
    }


def evaluate(s, p, result):
    child = result["proposal"]["child"]
    result["dev"] = development(s, p, child)
    library = StudyLibrary(s)
    dev = [s.tasks[t] for t in p["objects"] + p["config"]]
    support = [s.tasks[t] for t in sorted(library.provenance(child)["support"])]
    pairs = {(r["task"], r["repeat"]): r for r in result["dev"]}
    for name, parent in (("parent_gate", LEARNED), ("library_gate", None)):
        path = s.root / (name + ".json")
        if not path.exists():
            dump(path, library.evaluate(child, dev, support, workers=p["workers"], diagnostic_parent=parent))
        gate = read(path)
        require(
            gate["candidate"] == child
            and gate["baseline_members"] == ([LEARNED] if parent else [])
            and (
                parent
                or s.store.get_artifact(gate["receipt"]) == {k: v for k, v in gate.items() if k != "receipt"}
            ),
            "wrong gate binding",
        )
        for r in gate["rows"]:
            if r["split"] == "dev":
                pair = pairs[r["task_id"], r["repeat"]]
                require(
                    (r["baseline_run"], r["candidate_run"])
                    == (pair["parent" if parent else "bare"]["run"], pair["child"]["run"]),
                    "changed paired attempts",
                )
        result[name] = gate
    result["decision"] = release_decision(
        [r for r in result["parent_gate"]["rows"] if r["task_id"] in p["objects"]],
        result["parent_gate"],
        result["library_gate"],
    )
    if not result["decision"]["passed"]:
        result["phase"] = "gate_rejected"
        return
    gate = result["library_gate"]
    if s.store.active_library() is None:
        s.store.activate_library(gate["library_id"], gate["receipt"], expected=None, mode="live")
    require(s.store.active_library() == gate["library_id"], "child not activated")
    result.update(published=True, phase="frozen_holdout")
    dump(s.root / "progress.json", result)
    stop = threading.Event()
    for family in FAMILIES:

        def pair(item):
            i, tid = item
            task = s.tasks[tid]
            s.ledger.claim_test(tid, digest(task.model_dump()), s.protocol)
            row = library.compare_pair(task, 0, [], [child], child, reverse=bool(i % 2), stop=stop)
            return dict(row, family=family)

        rows = bounded_map(pair, list(enumerate(p["holdout"][family])), p["workers"])
        result["holdout"].extend(rows)
        result["curve"].append(curve_point(result["holdout"], family, rows))
        dump(s.root / "progress.json", result)
    result["phase"] = "complete"


def run(root, seed, cfg):
    p = read(root / "protocol.json")
    require(
        p == read(PUBLIC)
        and p["codes"] == sources()
        and p["settings"] == digest(cfg.public())
        and p["tasks"] == read(root / "dataset/manifest.json")["tasks"]
        and all(v == digest(read(OLD / (k + ".json"))) for k, v in p["published_observations"].items()),
        "registration, previous observations, code or settings changed",
    )
    ledger = Store(cfg.state_dir, unit=cfg.accounting_unit)
    with (ledger.root / "comparison.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        complete, start = root / "result.json", root / "start.json"
        if complete.exists():
            out = read(complete)
            require(out["completed"] and out["protocol"] == digest(p), "wrong completed study")
            return out
        if not start.exists():
            with ledger.connect() as db:
                cursor = db.execute("SELECT coalesce(max(rowid),0) FROM calls").fetchone()[0]
                prior = {r[0] for r in db.execute("SELECT task_id FROM test_exposures")}
            require(not prior.intersection(t for ts in p["holdout"].values() for t in ts), "exposed holdout")
            dump(start, {"protocol": digest(p), "cursor": cursor})
        require(read(start)["protocol"] == digest(p), "changed ledger cursor")
        store = (
            Store(root / "state", ledger=ledger)
            if (root / "state").exists()
            else clone_state(Archive(seed / "state/evocoder.sqlite3"), root / "state", ledger)
        )
        s = TransferStudy(root, cfg, store, ledger, p)
        require(
            set(StudyLibrary(s).provenance(LEARNED)["support"]) == set(p["root_support"]),
            "changed root lineage",
        )
        s.manual = Store(root / "manual-state", ledger=ledger)
        require(
            s.manual.artifact("skill", REFERENCE.model_dump()) == p["manual"], "changed manual comparator"
        )
        with s.manual.connect(write=True) as db:
            db.execute(
                "INSERT OR IGNORE INTO skills(id,parent,status,evidence_hash) VALUES(?,NULL,?,?)",
                (REFERENCE.hash, "CANDIDATE", digest("pretest-only-reference")),
            )
        out = {
            "protocol": s.protocol,
            "phase": "learning",
            "published": False,
            "dev": [],
            "holdout": [],
            "curve": [],
        }
        try:
            out["learn"] = learning_trials(s, p)
            out["evidence_runs"] = select_evidence(out["learn"])
            out["phase"] = "no_qualifying_application"
            if out["evidence_runs"]:
                out["proposal"] = propose(s, out["evidence_runs"])
                out["phase"] = "proposal_rejected"
                if out["proposal"]["child"]:
                    print("frozen child", out["proposal"]["child"], flush=True)
                    out["phase"] = "development"
                    dump(root / "progress.json", out)
                    evaluate(s, p, out)
            with ledger.connect() as db:
                exposed = {
                    r[0]
                    for r in db.execute(
                        "SELECT task_id FROM test_exposures WHERE experiment=?", (s.protocol,)
                    )
                }
            require(exposed == {r["task_id"] for r in out["holdout"]}, "unreported holdout access")
            out["holdout_exposures"] = len(exposed)
            out["completed"] = True
        finally:
            with ledger.connect() as db:
                charged = {
                    r[0]
                    for r in db.execute(
                        "SELECT DISTINCT run_id FROM calls WHERE rowid>?", (read(start)["cursor"],)
                    )
                }
            with store.connect() as db:
                proposals = {
                    r[0]
                    for r in db.execute(
                        "SELECT run_id FROM events WHERE kind='PROPOSAL_START' "
                        "AND json_extract(payload,'$.proposal_policy')=?",
                        (POLICY,),
                    )
                }
            slots = {f.stem for f in (root / "slots").glob("*.json")} | proposals
            out["costs"] = ledger.costs(sorted(charged & slots))
            out["unassigned"] = len(charged - slots)
            out["transport"] = [read(f) for f in sorted((root / "transport").glob("*.json"))]
            dump(root / "progress.json", out)
        require(not out["unassigned"], "unattributed charges")
        dump(complete, out)
        return out


if __name__ == "__main__":
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("command", choices=["prepare", "run"])
    cli.add_argument("--root", type=Path, default=Path("experiments/object-revision-001"))
    cli.add_argument("--panel", type=Path, default=Path("experiments/falsy-objects-001"))
    cli.add_argument("--seed", type=Path, default=Path("experiments/optional-control-001"))
    cli.add_argument("--env-file", type=Path, default=Path(".env.glm"))
    args = cli.parse_args()
    cfg = Settings.from_env_file(args.env_file)
    cfg.validate_live()
    require(
        cfg.provider == "glm"
        and cfg.model.lower() == "glm-5.3"
        and cfg.mode == "live"
        and cfg.max_steps == 16
        and cfg.skill_routing == "semantic"
        and cfg.image.startswith("sha256:"),
        "requires the registered GLM and solver contract",
    )
    if args.command == "prepare":
        print("frozen", digest(prepare(args.root, args.panel, args.seed, cfg)))
    else:
        report = run(args.root, args.seed, cfg)
        print(report["phase"], report["costs"])
