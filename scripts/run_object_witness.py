"""One preregistered sibling from a matched, model-written learn check. Never select by panel score."""

import argparse
import fcntl
from pathlib import Path

from evocoder.comparison import clone_state, dump
from evocoder.contracts import Settings, digest
from evocoder.errors import ProposalRejected
from evocoder.evolution import Evolution
from evocoder.model import Model
from evocoder.store import Store
from falsy_transfer_protocol import LEARNED, REFERENCE
from object_revision_cases import cases
from object_witness import WitnessProposer, draft, matched_learning, verdict
from report_click_procedure import Archive, read
from run_falsy_transfer import TransferStudy, atomic_trial, require
from run_object_revision import (
    POLICY,
    PUBLIC as FIRST_REGISTRATION,
    arm_run,
    evaluate,
    sources as first_sources,
)


HERE = Path(__file__).resolve().parent
PUBLIC = Path("benchmarks/falsy_transfer/object-witness.json")


def sources():
    return first_sources() | {
        n: digest((HERE / n).read_text()) for n in ("object_witness.py", "run_object_witness.py")
    }


def prepare(root, primary, cfg):
    require(not (root / "protocol.json").exists(), "already registered")
    old = read(primary / "protocol.json")
    before = read(primary / "progress.json")
    require(
        old == read(FIRST_REGISTRATION)
        and old["settings"] == digest(cfg.public())
        and before["protocol"] == digest(old)
        and before.get("proposal", {}).get("child"),
        "missing first revision",
    )
    match = matched_learning(before["learn"])
    p = {
        k: old[k]
        for k in (
            "tasks",
            "settings",
            "model",
            "image",
            "parent",
            "manual",
            "root_support",
            "objects",
            "config",
            "learn",
            "repeats",
            "workers",
            "solver_slots",
            "holdout",
            "object_arms",
            "config_arms",
            "published_observations",
            "calibration",
            "release",
            "test",
        )
    }
    p.update(
        study="caller-object-matched-public-witness-v1",
        codes=sources(),
        previous=digest(old),
        previous_child=before["proposal"]["child"],
        learning_receipts=digest(before["learn"]),
        matched=match,
        design="Adaptive follow-up after examining the first child's narrow content and part of its development "
        "panel. Only original learn runs are eligible evidence. The first child and all panel outcomes "
        "are withheld from both the check author and the new proposer.",
        entry="Only after the previous child has failed its unchanged gate with zero holdout exposure.",
        witness="Two actual forced-parent submissions on one task, both public-green, one independently failed. "
        "Blind the labels. GLM may write up to three standalone public checks. Use the first that raises "
        "an assertion on the failed submission and passes the successful submission and reference. "
        "Other exceptions do not count. Hidden tests and reference code stay evaluator-side.",
        proposal="One sibling from the original parent, matched learn episodes, three other source groups' "
        "successful applications and the executed public counterexample. At most one identical-input "
        "retry for a settled invalid format. No selection on content or dev scores.",
        routing_check="One ordinary semantic solve on each of all six learning tasks with the frozen sibling; "
        "diagnostic only. Rerun all armed dev comparisons regardless of routing frequency.",
        stop="This is the final proposal in this follow-up. A negative result preserves all thirty holdouts.",
    )
    # The data view is identical; use ordinary files so the experiment can be moved independently.
    import shutil

    shutil.copytree(primary / "dataset", root / "dataset", dirs_exist_ok=True)
    dump(root / "protocol.json", p)
    return p


def authored_check(s, p, match, task, ref):
    failed, good = (s.store.run(match[k]) for k in ("failure", "success"))
    require(
        all(
            r["purpose"] == "solve"
            and r["split"] == "learn"
            and r["task_hash"] == digest(task.model_dump())
            and r["skill_id"] == LEARNED
            for r in (failed, good)
        ),
        "changed matched parent applications",
    )
    pf, pg = (s.store.get_artifact(r["result"]["patch"]) for r in (failed, good))
    attempts = []
    for i in range(3):
        rid = "check-" + digest({"protocol": digest(p), "attempt": i})[:32]
        cache = s.root / "witness" / f"{i}.json"
        if cache.is_file():
            record = read(cache)
            require(record["run"] == rid, "invalid cached check")
        else:
            with s.ledger.connect() as db:
                existing = db.execute("SELECT 1 FROM calls WHERE run_id=?", (rid,)).fetchone()
            call_events = [e for e in s.store.events(rid) if e["kind"] == "CHECK_DRAFT"]
            if call_events:
                program = s.store.get_artifact(call_events[-1]["payload"]["artifact"])["program"]
            else:
                require(not existing, "original check request requires reconciliation")
                try:
                    program = draft(Model(s.settings, s.store), rid, task, [pf, pg])
                except (ValueError, SyntaxError) as exc:
                    cost = s.store.costs([rid])
                    require(cost["calls"] > 0 and not cost["reserved_credits"], "unsettled check draft")
                    record = {"run": rid, "accepted": False, "invalid": type(exc).__name__}
                    atomic_trial(cache, record)
                    attempts.append(record)
                    continue
                artifact = s.store.artifact("model_authored_public_check", {"program": program})
                s.store.event(rid, "CHECK_DRAFT", {"artifact": artifact})
            # The exact same source is executed against all three unrelated editable snapshots.
            statuses = {
                k: verdict(task, patch, program, s.settings)
                for k, patch in (("failure", pf), ("success", pg), ("reference", ref))
            }
            accepted = statuses == {"failure": "assertion_failed", "success": "passed", "reference": "passed"}
            record = {
                "run": rid,
                "program": program,
                "hash": digest(program),
                "statuses": statuses,
                "accepted": accepted,
            }
            atomic_trial(cache, record)
        attempts.append(record)
        if record["accepted"]:
            return attempts, {
                "task": task.id,
                "task_hash": digest(task.model_dump()),
                "program": record["program"],
                "program_hash": record["hash"],
                "failure_run": failed["id"],
                "success_run": good["id"],
                "failure_patch": failed["result"]["patch"],
                "success_patch": good["result"]["patch"],
                "on_failure": record["statuses"]["failure"],
                "on_success": record["statuses"]["success"],
            }
    return attempts, None


def sibling(s, refs, obs):
    engine = Evolution(
        s.settings,
        s.store,
        model=WitnessProposer(s.settings, s.store, obs),
        feedback_policy="learn_outcome",
        proposal_policy=POLICY,
        grounding_tasks={t.id: t for t in s.tasks.values() if t.split == "learn"},
    )
    h, _ = engine.freeze(refs, LEARNED)
    while True:
        with s.store.connect() as db:
            ids = [
                r[0]
                for r in db.execute(
                    "SELECT run_id FROM events WHERE kind='PROPOSAL_START' "
                    "AND json_extract(payload,'$.evidence_hash')=? ORDER BY rowid",
                    (h,),
                )
            ]
        for rid in ids:
            events = s.store.events(rid)
            require(
                any(
                    e["kind"] == "PUBLIC_LEARN_CONTRAST"
                    and s.store.get_artifact(e["payload"]["receipt"]) == obs
                    for e in events
                ),
                "changed proposal type or witness",
            )
            if events[-1]["kind"] == "PROPOSED":
                key = events[-1]["payload"]["skill_id"]
                receipt = next(e["payload"]["artifact"] for e in events if e["kind"] == "SUMMARY_VERIFIED")
                return {"child": key, "evidence": h, "receipt": receipt, "attempts": ids}
            require(
                events[-1]["kind"] == "PROPOSAL_ERROR"
                and s.store.costs([rid])["calls"] > 0
                and not s.store.costs([rid])["reserved_credits"]
                and not s.ledger.budget()["blocked"],
                "unsettled original proposal",
            )
        if len(ids) >= 2:
            return {"child": None, "evidence": h, "receipt": None, "attempts": ids}
        try:
            engine.propose(refs, LEARNED)
        except ProposalRejected:
            continue


def run(root, primary, cfg):
    p = read(root / "protocol.json")
    prior = read(primary / "result.json")
    require(
        p == read(PUBLIC)
        and p["codes"] == sources()
        and p["settings"] == digest(cfg.public())
        and p["tasks"] == read(root / "dataset/manifest.json")["tasks"]
        and prior["protocol"] == p["previous"]
        and prior["proposal"]["child"] == p["previous_child"]
        and prior["phase"] == "gate_rejected"
        and prior["completed"]
        and not prior["published"]
        and not prior["holdout_exposures"]
        and digest(prior["learn"]) == p["learning_receipts"]
        and matched_learning(prior["learn"]) == p["matched"],
        "previous study did not authorize witness follow-up",
    )
    ledger = Store(cfg.state_dir, unit=cfg.accounting_unit)
    with (ledger.root / "comparison.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        done, start = root / "result.json", root / "start.json"
        if done.exists():
            result = read(done)
            require(result["completed"] and result["protocol"] == digest(p), "wrong terminal result")
            return result
        if not start.exists():
            with ledger.connect() as db:
                cursor = db.execute("SELECT coalesce(max(rowid),0) FROM calls").fetchone()[0]
                exposed = {r[0] for r in db.execute("SELECT task_id FROM test_exposures")}
            require(
                not exposed.intersection(t for ts in p["holdout"].values() for t in ts), "holdout not fresh"
            )
            dump(start, {"protocol": digest(p), "cursor": cursor})
        cursor = read(start)
        require(cursor["protocol"] == digest(p), "wrong accounting cursor")
        store = (
            Store(root / "state", ledger=ledger)
            if (root / "state").exists()
            else clone_state(Archive(primary / "state/evocoder.sqlite3"), root / "state", ledger)
        )
        s = TransferStudy(root, cfg, store, ledger, p)
        s.manual = Store(root / "manual-state", ledger=ledger)
        require(s.manual.artifact("skill", REFERENCE.model_dump()) == p["manual"], "changed manual procedure")
        with s.manual.connect(write=True) as db:
            db.execute(
                "INSERT OR IGNORE INTO skills(id,parent,status,evidence_hash) VALUES(?,NULL,?,?)",
                (REFERENCE.hash, "CANDIDATE", digest("pretest-only-reference")),
            )
        match = p["matched"]
        ref = next(ref for t, ref in cases() if t.id == match["task"])
        out = {
            "protocol": s.protocol,
            "phase": "public_witness",
            "dev": [],
            "routing_trials": [],
            "published": False,
            "holdout": [],
            "curve": [],
            "evidence_runs": match["refs"],
        }
        try:
            out["witness_attempts"], out["witness"] = authored_check(s, p, match, s.tasks[match["task"]], ref)
            out["phase"] = "no_distinguishing_public_check"
            if out["witness"]:
                print("validated public witness", out["witness"]["program_hash"], flush=True)
                out["proposal"] = sibling(s, match["refs"], out["witness"])
                out["phase"] = "proposal_rejected"
                if child := out["proposal"]["child"]:
                    print("frozen sibling", child, flush=True)
                    require(child != p["previous_child"], "proposal unexpectedly identical")
                    out["phase"] = "development"
                    dump(root / "progress.json", out)
                    for tid in p["learn"]:
                        # These are evaluator runs. They cannot enter this or any later learning proposal.
                        out["routing_trials"].append(
                            {
                                "task": tid,
                                "child": arm_run(s, s.tasks[tid], "scope-diagnostic", 0, "child", child),
                            }
                        )
                    evaluate(s, p, out)
            with ledger.connect() as db:
                exposed = {
                    r[0]
                    for r in db.execute(
                        "SELECT task_id FROM test_exposures WHERE experiment=?", (s.protocol,)
                    )
                }
            require(exposed == {r["task_id"] for r in out["holdout"]}, "unreported holdout")
            out.update(holdout_exposures=len(exposed), completed=True)
        finally:
            with ledger.connect() as db:
                charged = {
                    r[0]
                    for r in db.execute(
                        "SELECT DISTINCT run_id FROM calls WHERE rowid>?", (cursor["cursor"],)
                    )
                }
            with store.connect() as db:
                props = {
                    r[0] for r in db.execute("SELECT run_id FROM events WHERE kind='PUBLIC_LEARN_CONTRAST'")
                }
            identifiers = (
                {f.stem for f in (root / "slots").glob("*.json")}
                | props
                | {"check-" + digest({"protocol": s.protocol, "attempt": i})[:32] for i in range(3)}
            )
            out["costs"] = ledger.costs(sorted(identifiers & charged))
            out["unassigned"] = len(charged - identifiers)
            out["transport"] = [read(f) for f in sorted((root / "transport").glob("*.json"))]
            dump(root / "progress.json", out)
        require(not out["unassigned"], "unattributed expenses")
        dump(done, out)
        return out


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["prepare", "run"])
    parser.add_argument("--root", type=Path, default=Path("experiments/object-witness-001"))
    parser.add_argument("--primary", type=Path, default=Path("experiments/object-revision-001"))
    parser.add_argument("--env-file", type=Path, default=Path(".env.glm"))
    args = parser.parse_args()
    cfg = Settings.from_env_file(args.env_file)
    cfg.validate_live()
    if args.command == "prepare":
        print("frozen", digest(prepare(args.root, args.primary, cfg)))
    else:
        out = run(args.root, args.primary, cfg)
        print(out["phase"], out["costs"])
