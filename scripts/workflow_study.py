"""Register independent learning, collect GLM repairs and bind every arm to its own ledger slot."""

import argparse
import fcntl
from contextlib import contextmanager
from pathlib import Path

from callback_protocol import PUBLIC as PRIOR_PROTOCOL, RESULT as PRIOR_RESULT, ROOT as SEED, old_task
from callback_sources import sources as earlier_sources
from evocoder.comparison import clone_state, frozen_knowledge, sources
from evocoder.contracts import digest
from evocoder.jobs import bounded_map
from evocoder.route_shape import public_test_shape
from evocoder.runtime import Runner, validation
from evocoder.store import Store
from falsy_transfer_protocol import probe_patch
from portfolio_routing import CHILD, PARENT, config
from report_click_procedure import Archive, read
from run_falsy_transfer import atomic_trial, require
from workflow_cases import cases

ROOT = Path("experiments/workflow-revision-001")
PUBLIC = Path("benchmarks/falsy_transfer/workflow-revision.json")
RESULT = Path("docs/results/workflow-revision.json")
FLAGS = ("badge-alerts", "preview-toolbar")
WAKES = ("harbor-slots", "mixer-restart")
FAMILIES = (("journal-cursor", "watermark-bus"), ("rollback-batch", "compensation-stack"))
LEARN = FLAGS + WAKES
WORKERS = 4


def sources_hash():
    files = (
        "workflow_cases.py",
        "workflow_study.py",
        "workflow_learning.py",
        "object_revision_cases.py",
        "callback_policy.py",
        "callback_protocol.py",
        "callback_sources.py",
        "falsy_transfer_protocol.py",
        "portfolio_routing.py",
        "run_falsy_transfer.py",
    )
    return {
        "runtime": digest(sources()),
        **{f: digest(Path(__file__).with_name(f).read_text()) for f in files},
    }


def public_check(task, patch, program, cfg):
    result = validation(
        {**task.files, **patch, "learning_feedback.py": program},
        ["python", "-B", "learning_feedback.py"],
        cfg,
        task.id + "-learncheck",
    )
    require(
        result["image_id"] == cfg.image and not result["timeout"] and result["exit_code"] in (0, 1),
        "public learning feedback infrastructure failed",
    )
    return dict(
        program=digest(program),
        passed=result["exit_code"] == 0,
        observation=result["output"].strip().splitlines()[-1:],
    )


def register(cfg):
    old, result = read(PRIOR_PROTOCOL), read(PRIOR_RESULT)
    seed = Archive(SEED / "state/evocoder.sqlite3")
    require(
        result["protocol"] == digest(old)
        and not result["published"]
        and result["holdout_exposures"] == 0
        and seed.active_library() is None
        and old["settings"] == digest(cfg.public()),
        "wrong study anchor",
    )
    new = cases()

    def calibrate(item):
        tid, (t, ref, program) = item
        broken, fixed = [probe_patch(t, patch, cfg) for patch in ({}, ref)]
        require(not broken["public"] and all(fixed.values()), "invalid task " + tid)
        record = dict(
            id=tid,
            group=t.source_group,
            split=t.split,
            snapshot=digest(t.model_dump()),
            reference=digest(ref),
            shapes=public_test_shape(t),
            broken=broken,
            fixed=fixed,
        )
        if program:
            record["feedback"] = digest(program)
            require(
                not public_check(t, {}, program, cfg)["passed"]
                and public_check(t, ref, program, cfg)["passed"],
                "invalid public contract exercise " + tid,
            )
        else:
            partial = probe_patch(t, {t.editable[0]: ref[t.editable[0]]}, cfg)
            require(
                partial["public"]
                and partial["controls"]
                and not all(partial.values())
                and any(x.startswith("assert Is:") for x in record["shapes"]),
                "invalid sibling defect " + tid,
            )
            record["partial"] = partial
        return record

    registered = bounded_map(calibrate, list(new.items()), WORKERS)
    earlier = {v["confirmation"]["id"]: v["confirmation"] for v in old["families"].values()}
    protected = old["protected"] | earlier
    require(
        len({t.source_group for t, _, _ in new.values()}) == len(new)
        and not set(new).intersection(
            protected,
        )
        and not set(new).intersection(old["holdout_hashes"])
        and not {t.source_group for t, _, _ in new.values()}.intersection(
            m["group"] for m in protected.values()
        ),
        "task or group overlaps",
    )
    p = dict(
        study="independent-workflow-revision-v1",
        previous=digest(result),
        previous_receipt=result["gate"]["receipt"],
        previous_protocol=digest(old),
        ancestral_knowledge=frozen_knowledge(seed),
        model=cfg.model,
        settings=digest(cfg.public()),
        image=cfg.image,
        root=PARENT,
        old_child=CHILD,
        code=sources_hash(),
        programs={m["id"]: m for m in registered},
        flags=list(FLAGS),
        wakes=list(WAKES),
        sources=[list(x) for x in FAMILIES],
        protected=protected,
        old_objects=old["objects"] + list(earlier),
        config=old["config"],
        support=old["support"],
        noise=old["noise"],
        holdout=old["holdout"],
        holdout_hashes=old["holdout_hashes"],
        workers=WORKERS,
        solver_actions=15,
        learning="4 normal old-portfolio solves per new learn task, plus 2 fixed old-child solves per wake task. "
        "If either target exposure is missing, take up to 4 further rounds on deficient target(s) only, "
        "normal for flags, normal and fixed for wakes. Freeze earliest actual flag misroute plus actual submitted "
        "wake-path failure with same-run public-contract evidence and child applied. Include matched successful "
        "contrasts and additional independent failures, at most six refs. At most two settled proposal attempts. "
        "Both failed mechanisms must be represented; no dev or oracle inputs to proposer.",
        screen="Fixed sequence of independent callback source pairs. Five fresh original-parent/issue solves on each "
        "implementation. Both must have at least three submitted public- and control-green patches missing a named "
        "callback boundary. Stop screening after first eligible pair. Screening outcomes cannot count as gate wins.",
        gate="Candidate and evaluation code pinned before candidate sees development programs. Original label and "
        "the second implementation of the first eligible source each need >=3/5 changed-request, newly selected "
        "child wins against BOTH bare and original parent. Other four previously observed object tasks protected 5 rounds. "
        "Four original configurations protected 7 rounds with previous raw/changed/round-block veto. Replay union "
        "of provenance learning tasks three times, scheduler five. All compare bare, parent/issue, revised/public_shape. "
        "Also directly compare previous/public_shape on label, new target, all configurations and scheduler. On "
        "print/scheduler require no increase in wrong child selections on print and no loss in aggregate controls/wake "
        "probe passes relative to previous. Unscored/incomplete attempts cannot authorize publication.",
        fallback="When no source qualifies or no child is produced there is no formal efficacy gate. If a child "
        "exists, collect protected four-configuration (seven rounds) and scheduler (five rounds) previous/revised "
        "comparisons even when source screening fails. Report separately from release.",
        test=old["test_rule"],
        order_seed="workflow-revision-v1",
        interruptions=old["interruptions"],
    )
    atomic_trial(ROOT / "protocol.json", p)
    atomic_trial(PUBLIC, p)
    return p


class Study:
    def __init__(self, p, cfg, ledger, store):
        self.p, self.cfg, self.ledger, self.store = p, cfg, ledger, store
        self.protocol = digest(p)
        self.new = cases()
        self.previous = {t.id: t for parts in earlier_sources().values() for t, _ in parts.values()}

    def activation(self):
        a = read(ROOT / "activation.json")
        report = self.store.get_artifact(a["receipt"])
        require(
            report["protocol"] == self.protocol
            and report["gate"]["passed"] is True
            and self.store.active_library() == a["library"] == report["library_id"]
            and frozen_knowledge(self.store) == a["knowledge"]
            and self.store.library_members(a["library"], mode="live", require_published=True) == a["members"],
            "holdout requires an authentic active library",
        )
        return a

    def task(self, tid):
        if tid in self.new:
            t = self.new[tid][0]
            expected = self.p["programs"][tid]["snapshot"]
        elif tid in self.p["holdout_hashes"]:
            self.activation()
            expected = self.p["holdout_hashes"][tid]
            t = old_task(tid)
        else:
            expected = self.p["protected"][tid]["snapshot"]
            t = self.previous[tid] if tid in self.previous else old_task(tid)
        require(digest(t.model_dump()) == expected, "frozen program diverged")
        return t

    def solve(self, phase, tid, n, arm, child=None):
        t = self.task(tid)
        learning = phase == "learn"
        require(not learning or tid in LEARN, "non-learning evidence")
        require(
            arm != "revised"
            or child
            and child != CHILD
            and self.store.get_artifact(child)["parent"] == CHILD,
            "revision has wrong ancestry",
        )
        pool = dict(
            bare=[],
            parent=[PARENT],
            previous=sorted([PARENT, CHILD]),
            fixed=[CHILD],
            revised=sorted([PARENT, child]) if child else [],
        )[arm]
        view = "public_shape" if arm in {"previous", "revised"} else "issue"
        cfg = self.cfg.model_copy(update={"library_route_context": view, "max_steps": 16 if pool else 15})
        active = None
        if t.split == "test":
            require(phase == "holdout" and arm in ("bare", "revised") and n == 0, "wrong test assignment")
            active = self.activation()
            require(not pool or pool == active["members"], "different published skill pool")
            self.ledger.claim_test(t.id, digest(t.model_dump()), self.protocol)
        else:
            require(self.store.active_library() is None and phase != "holdout", "deployment changed")
        intent = dict(
            protocol=self.protocol,
            phase=phase,
            task=tid,
            repeat=n,
            arm=arm,
            pool=pool,
            view=view,
            options=digest(cfg.public()),
            snapshot=digest(t.model_dump()),
            library=active["library"] if active and pool else None,
            fixed=arm == "fixed",
            purpose="solve" if learning else "evaluate",
        )
        rid = digest(intent)[:32]
        atomic_trial(ROOT / "intents" / (rid + ".json"), intent)
        path = ROOT / "rows" / (rid + ".json")
        if path.exists():
            row = read(path)
            require(row["intent"] == intent and row["run"] == rid, "different paid slot")
            return row
        with self.store.connect() as db:
            exists = db.execute("SELECT 1 FROM runs WHERE id=?", (rid,)).fetchone()
        if not exists:
            args = {"library_id": intent["library"]} if intent["library"] else {"skill_ids": pool}
            Runner(cfg, self.store).solve(
                t,
                use_active=False,
                candidate=learning,
                purpose=intent["purpose"],
                run_id=rid,
                fixed_route="apply" if arm == "fixed" else None,
                **args,
            )
        r, ev = self.store.run(rid), self.store.events(rid)
        out = r["result"]
        require(
            r["state"] in ("COMPLETE", "FAILED", "EXHAUSTED", "ERROR", "INTERRUPTED"),
            "paid slot still active",
        )
        require(
            r["mode"] == "live"
            and r["purpose"] == intent["purpose"]
            and r["task_hash"] == intent["snapshot"]
            and r["split"] == t.split,
            "wrong source or role",
        )
        start = next(e["payload"] for e in ev if e["kind"] == "START")
        require(
            start["requested_skill_ids"] == pool
            and start.get("fixed_route") == ("apply" if arm == "fixed" else None)
            and start.get("route_context", "issue") == view
            and start["library_id"] == intent["library"],
            "arm mismatch",
        )
        responses = [e["payload"] for e in ev if e["kind"] == "MODEL_RESPONSE"]
        actions = [e for e in responses if e["expected_function"] == "submit_action"]
        routers = [e for e in responses if e["expected_function"] == "select_library_skill"]
        route = next((e["payload"] for e in ev if e["kind"] == "SKILL_ROUTE"), {})
        cost = self.store.costs([rid])
        graded = r["state"] in ("COMPLETE", "FAILED", "EXHAUSTED") and not cost["reserved_credits"]
        require(
            all(e["provider_model"].lower() == cfg.model.lower() for e in responses)
            and len(actions) <= 15
            and len(routers) <= int(bool(pool) and arm != "fixed"),
            "wrong model or budget",
        )
        if graded:
            require(
                out["image_id"] == cfg.image
                and len(actions) > 0
                and len(routers) == int(bool(pool) and arm != "fixed")
                and route["applied_skill_id"] == r["skill_id"]
                and r["skill_id"] in [None, *pool],
                "incomplete execution",
            )
        row = dict(
            intent=intent,
            run=rid,
            state=r["state"],
            passed=out.get("passed") if graded else None,
            selected=r["skill_id"],
            route=route.get("decision"),
            reason=route.get("reason"),
            first_solver_request=actions[0]["request_body_hash"] if actions else None,
            router_request=routers[0]["request_body_hash"] if routers else None,
            solver_calls=len(actions),
            patch=out.get("patch"),
            probes=None,
            public_feedback=None,
            cost=cost,
        )
        if row["patch"]:
            patch = self.store.get_artifact(row["patch"])
            if any(k.startswith("probe_") for k in t.grader_files):
                row["probes"] = probe_patch(t, patch, cfg)
                require(all(row["probes"].values()) == row["passed"], "patch replay disagrees")
            if learning:
                row["public_feedback"] = public_check(t, patch, self.new[tid][2], cfg)
        atomic_trial(path, row)
        print(phase, tid, n + 1, arm, r["state"], str(row["selected"])[:7], row["probes"], flush=True)
        return row


@contextmanager
def session():
    cfg = config(Path(".env.glm"))
    p = read(PUBLIC)
    require(
        p == read(ROOT / "protocol.json")
        and p["code"] == sources_hash()
        and p["settings"] == digest(cfg.public())
        and p["previous"] == digest(read(PRIOR_RESULT)),
        "protocol/runtime/provider drift",
    )
    ledger = Store(cfg.state_dir, unit=cfg.accounting_unit)
    with (ledger.root / "comparison.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if not (ROOT / "start.json").exists():
            with ledger.connect() as db:
                prior = {r[0] for r in db.execute("SELECT task_id FROM test_exposures")}
                require(not prior.intersection(p["holdout_hashes"]), "holdout previously exposed")
                anchor = db.execute("SELECT coalesce(max(rowid),0) FROM calls").fetchone()[0]
                reserved = [
                    tuple(r)
                    for r in db.execute("SELECT id,reserved FROM calls WHERE state='reserved' ORDER BY rowid")
                ]
            atomic_trial(
                ROOT / "start.json",
                dict(
                    protocol=digest(p),
                    cursor=anchor,
                    prior_reservations=len(reserved),
                    reservations_hash=digest(reserved),
                ),
            )
        require(read(ROOT / "start.json")["protocol"] == digest(p), "wrong ledger cursor")
        path = ROOT / "state"
        if path.exists():
            store = Store(path, ledger=ledger)
        else:
            seed = Archive(SEED / "state/evocoder.sqlite3")
            require(frozen_knowledge(seed) == p["ancestral_knowledge"], "source knowledge changed")
            store = clone_state(seed, path, ledger)
        yield Study(p, cfg, ledger, store)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=["register", "learn", "screen"])
    args = parser.parse_args()
    if args.phase == "register":
        print("registered", digest(register(config(Path(".env.glm")))), flush=True)
    else:
        from workflow_learning import learn, screen

        with session() as s:
            (learn if args.phase == "learn" else screen)(s)
