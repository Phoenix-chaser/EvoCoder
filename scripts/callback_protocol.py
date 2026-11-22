"""Register the complete callback experiment before drawing any new GLM outcomes."""

import argparse
import copy
import fcntl
import itertools
import random
from contextlib import contextmanager
from pathlib import Path

from callback_policy import calibrate_noise, pilot_summary
from callback_sources import sources as callback_sources
from evocoder.comparison import clone_state, frozen_knowledge, sources
from evocoder.contracts import Task, digest
from evocoder.jobs import bounded_map
from evocoder.library import SkillLibrary
from evocoder.route_shape import public_test_shape
from evocoder.runtime import Runner
from evocoder.store import Store
from falsy_transfer_protocol import FAMILIES, probe_patch
from portfolio_routing import CHILD, PARENT, POOLS, config
from report_click_procedure import Archive, read
from run_falsy_transfer import atomic_trial, require
from run_optional_control import Study
from run_parallel_routing import read_arm
from shape_study import PUBLIC as PREVIOUS_PROTOCOL
from shape_study import RESULT as PREVIOUS_RESULT
from shape_study import ROOT as SEED

ROOT = Path("experiments/callback-release-001")
PUBLIC = Path("benchmarks/falsy_transfer/callback-release.json")
RESULT = Path("docs/results/callback-release.json")
ARMS = ("bare", "parent", "portfolio")
SOURCE_FILES = (
    "callback_sources.py",
    "callback_policy.py",
    "callback_protocol.py",
    "run_callback_release.py",
    "run_shape_routing.py",
    "run_optional_control.py",
    "run_parallel_routing.py",
    "falsy_transfer_protocol.py",
    "object_revision_cases.py",
    "portfolio_routing.py",
)
SCHEDULE_SEED = "callback-release-v1-balanced-six"


def code_hashes():
    return {
        "runtime": digest(sources()),
        **{f: digest(Path(__file__).with_name(f).read_text()) for f in SOURCE_FILES},
    }


def task_meta(t):
    return dict(id=t.id, group=t.source_group, split=t.split, snapshot=digest(t.model_dump()))


def old_task(tid):
    t = Task.model_validate(read(SEED / "dataset" / (tid + ".json")))
    require(t.id == tid, "different seed input")
    return t


def prepare(cfg):
    prior, previous = read(PREVIOUS_RESULT), read(PREVIOUS_PROTOCOL)
    require(
        previous == read(SEED / "protocol.json")
        and digest(previous) == prior["protocol"]
        and not prior["published"]
        and not prior["holdout_exposures"]
        and read(SEED / "result.json")["gate"]["receipt"] == prior["gate"]["receipt"]
        and previous["settings"] == digest(cfg.public()),
        "previous study or provider settings changed",
    )
    seed = Archive(SEED / "state/evocoder.sqlite3")
    require(
        seed.active_library() is None
        and SkillLibrary(cfg, seed, runner=object()).provenance(CHILD)["support"] == previous["support"],
        "different active library or ancestry",
    )
    membership = read(SEED / "dataset/manifest.json")["tasks"]
    old_hashes = {x["id"]: x["hash"] for x in membership}
    protected = {
        t: task_meta(old_task(t))
        for t in [*previous["objects"], *previous["config"], *sorted(previous["support"])]
    }
    require(all(v["snapshot"] == old_hashes[k] for k, v in protected.items()), "different original panel")
    registrations = {}

    def calibrate(item):
        t, ref = item
        direct = {path: ref[path] if i == 0 else t.files[path] for i, path in enumerate(t.editable)}
        broken, partial, fixed = [probe_patch(t, patch, cfg) for patch in ({}, direct, ref)]
        require(
            not broken["public"]
            and all(fixed.values())
            and partial["public"]
            and partial["controls"]
            and any(not val for k, val in partial.items() if k not in {"public", "controls"})
            and any(s.startswith("assert Is:") for s in public_test_shape(t)),
            "failed workspace or sibling-defect calibration",
        )
        return {
            **task_meta(t),
            "reference": digest(ref),
            "first_file_only": digest(direct),
            "test_shapes": digest(public_test_shape(t)),
            "calibration": {"broken": broken, "first_file_only": partial, "reference": fixed},
        }

    base = callback_sources()
    for family, stages in base.items():
        meta = bounded_map(calibrate, list(stages.values()), 2)
        registrations[family] = dict(zip(stages, meta, strict=True))
    all_new = [m for group in registrations.values() for m in group.values()]
    require(
        len(set(m["group"] for m in all_new)) == len(all_new)
        and not {m["id"] for m in all_new}.intersection(old_hashes),
        "duplicate source or renamed historical task",
    )
    held = previous["holdout"]
    require(
        list(held) == FAMILIES
        and all(len(g) == 10 for g in held.values())
        and len({t for g in held.values() for t in g}) == 30,
        "different original diversity protocol",
    )
    return dict(
        study="prospective-callback-release-v1",
        parent=PARENT,
        child=CHILD,
        model=cfg.model,
        image=cfg.image,
        settings=digest(cfg.public()),
        ancestral_knowledge=frozen_knowledge(seed),
        historical_gate=prior["gate"]["receipt"],
        previous_result=digest(prior),
        previous_protocol=digest(previous),
        code=code_hashes(),
        families=registrations,
        objects=previous["objects"],
        config=previous["config"],
        support=sorted(previous["support"]),
        protected=protected,
        holdout=held,
        holdout_hashes={t: old_hashes[t] for g in held.values() for t in g},
        noise=calibrate_noise(prior, previous["config"]),
        schedule_seed=SCHEDULE_SEED,
        workers=4,
        pilot_rule="Two independent pilot implementations, parent only, five fresh attempts each. Eligibility requires >=3/5 "
        "submitted public- and control-green patches failing a named caller-object boundary. Publish all attempts. Both distinct "
        "confirmation implementations are still run; an ineligible new family cannot count toward release.",
        release_rule="Freeze the learned library and structured router. All three existing object tasks and two new confirmation "
        "tasks run five times. Each original configuration task runs seven times and each of eight ancestry supports three times. "
        "Each cell runs three independent controls: bare, original parent/issue, original parent+child/public_shape. All receive "
        "15 solver actions. For EACH comparator require two distinct object sources with >=3/5 actual child wins and different "
        "first-solver request, including one eligible new family. Changed-wire paired net on every task must be nonnegative. Raw "
        "loss majority veto remains for objects/support. For config apply registered calibrated raw loss count and the adverse "
        "seven-block sign-flip test. Ties count in denominators. No imputation, no score-based reruns, no evidence borrowing. "
        "Missing, unsettled or infrastructure-invalid current attempts cannot authorize activation.",
        noise_rule="Use exclusively the 20 historical config pairs comparing original parent with issue-only portfolio, all with "
        "the same first-solver request. Exact one-sided 95% upper bound on discordance, half under exchangeability. Choose the "
        "smallest k>=4 with Binomial(7,q_upper/2) upper tail <=.025/8. Also veto a one-sided adverse sum of configuration "
        "differences at <=.025/2 using all 128 signs over seven round blocks. These bounds depend on stationarity, symmetry "
        "and repeat independence; they are engineering thresholds, not a guarantee against stochastic errors.",
        test_rule="Only after evaluator-issued live CAS publication. Claim the original 30 frozen configuration tasks once, "
        "one bare and one solve via the actual published library per task. Ten scalar, ten layered, ten batch; three equal-weight "
        "cumulative gains plus stratum gains. Zero learning or adaptive repeats; report any curve shape. No test inference on rejection.",
        interruptions="Persistent unique intents. Reuse identical terminal slots; never relabel an interrupted or unsettled attempt. "
        "Short batches release the worker between rounds. Historical reservations stay in the original shared ledger and are not billed to this protocol.",
    )


def arm_order(tid, n, arms):
    perms = list(itertools.permutations(arms))
    rnd = random.Random(digest([SCHEDULE_SEED, tid, n // len(perms), list(arms)]))
    rnd.shuffle(perms)
    return list(perms[n % len(perms)])


class ReleaseStudy(Study):
    def __init__(self, root, cfg, store, ledger, p):
        self.root, self.settings, self.store, self.ledger, self.protocol = root, cfg, store, ledger, digest(p)
        self.selected = "public_shape"
        self.p = p
        self.new = {t.id: t for stages in callback_sources().values() for t, _ in stages.values()}

    def task(self, tid):
        if tid in self.new:
            return self.new[tid]
        if tid in self.p["holdout_hashes"]:
            validate_activation(self)
        t = old_task(tid)
        key = self.p["holdout_hashes"].get(tid) or self.p["protected"][tid]["snapshot"]
        require(digest(t.model_dump()) == key, "snapshot mismatch")
        return t

    def solve(self, task, phase, repeat, *, pool, view):
        cfg = self.settings.model_copy(update={"library_route_context": view})
        active = None
        if task.split == "test":
            active = validate_activation(self)
            require(
                phase == "holdout"
                and task.id in self.p["holdout_hashes"]
                and repeat == 0
                and pool in ([], POOLS["portfolio"]),
                "undeclared holdout treatment",
            )
            self.ledger.claim_test(task.id, digest(task.model_dump()), self.protocol)
        else:
            require(phase == "gate" and self.store.active_library() is None, "changed pre-release deployment")
            if any(m["pilot"]["id"] == task.id for m in self.p["families"].values()):
                require(pool == [PARENT] and 0 <= repeat < 5, "pilot can use only the original parent")
                phase = "pilot"
        if not (active and pool):
            scoped = copy.copy(self)
            scoped.settings = cfg
            return Study.solve(scoped, task, phase, repeat, pool=pool)
        intent = dict(
            protocol=self.protocol,
            phase="published-holdout",
            task=task.id,
            task_hash=digest(task.model_dump()),
            repeat=repeat,
            pool=pool,
            oracle=False,
            options=digest(cfg.public()),
            library=active,
        )
        rid = digest(intent)[:32]
        atomic_trial(self.root / "slots" / (rid + ".json"), intent)
        with self.store.connect() as db:
            exists = db.execute("SELECT 1 FROM runs WHERE id=?", (rid,)).fetchone()
        if not exists:
            Runner(cfg, self.store).solve(
                task, use_active=False, library_id=active, purpose="evaluate", run_id=rid
            )
        run = self.store.run(rid)
        require(
            run["state"] in {"COMPLETE", "FAILED", "EXHAUSTED"} and run["result"]["library_id"] == active,
            "unfinished or unbound published-library solve",
        )
        return rid


def validate_activation(s):
    path = s.root / "activation.json"
    require(path.exists(), "test remains sealed pending a live activation receipt")
    a = read(path)
    gate = s.store.get_artifact(a["receipt"])
    require(
        a["protocol"] == s.protocol == gate["protocol"]
        and a["library"] == s.store.active_library() == gate["library_id"]
        and gate["gate"]["passed"]
        and s.store.library_members(a["library"], mode="live", require_published=True) == POOLS["portfolio"]
        and frozen_knowledge(s.store) == a["knowledge"],
        "foreign release or changed knowledge",
    )
    return a["library"]


@contextmanager
def session(p, cfg):
    require(
        read(PUBLIC) == read(ROOT / "protocol.json") == p
        and p["code"] == code_hashes()
        and p["settings"] == digest(cfg.public()),
        "registered runtime, configuration or protocol differs",
    )
    ledger = Store(cfg.state_dir, unit=cfg.accounting_unit)
    with (ledger.root / "comparison.lock").open("a") as fp:
        fcntl.flock(fp, fcntl.LOCK_EX | fcntl.LOCK_NB)
        start = ROOT / "start.json"
        if not start.exists():
            with ledger.connect() as db:
                old = {r[0] for r in db.execute("SELECT task_id FROM test_exposures")}
                require(not old.intersection(p["holdout_hashes"]), "previous test exposure")
                cursor = db.execute("SELECT coalesce(max(rowid),0) FROM calls").fetchone()[0]
                outstanding = [
                    tuple(x)
                    for x in db.execute("SELECT id,reserved FROM calls WHERE state='reserved' ORDER BY rowid")
                ]
            atomic_trial(
                start,
                {
                    "protocol": digest(p),
                    "cursor": cursor,
                    "old_reservations_hash": digest(outstanding),
                    "old_reservations": len(outstanding),
                },
            )
        require(read(start)["protocol"] == digest(p), "ledger anchor mismatch")
        path = ROOT / "state"
        if path.exists():
            state = Store(path, ledger=ledger)
        else:
            seed = Archive(SEED / "state/evocoder.sqlite3")
            require(frozen_knowledge(seed) == p["ancestral_knowledge"], "historical skill state changed")
            state = clone_state(seed, path, ledger)
        yield ReleaseStudy(ROOT, cfg, state, ledger, p)


def cell(s, phase, tid, n, arms):
    t = s.task(tid)
    seq = arm_order(tid, n, arms)
    r = dict(
        task=tid, snapshot=digest(t.model_dump()), group=t.source_group, split=t.split, repeat=n, order=seq
    )
    file = s.root / "trials" / f"{phase}-{tid}-{n}.json"
    if file.exists():
        prev = read(file)
        require(all(prev[k] == v for k, v in r.items()) and set(prev) == set(r) | set(seq), "changed trial")
        return prev
    for arm in seq:
        r[arm] = read_arm(s, t, n, arm)
    atomic_trial(file, r)
    print(phase, n + 1, tid, [(a, r[a]["passed"], str(r[a]["selected"])[:7]) for a in seq], flush=True)
    return r


def pilot_rows(s):
    return [
        read(s.root / "trials" / f"pilot-{f['pilot']['id']}-{n}.json")
        for n in range(5)
        for f in s.p["families"].values()
    ]


def pilot(s):
    ids = [f["pilot"]["id"] for f in s.p["families"].values()]
    bounded_map(
        lambda z: cell(s, "pilot", z[1], z[0], ("parent",)),
        [(n, t) for n in range(5) for t in ids],
        s.p["workers"],
    )
    decision = pilot_summary(s.p, pilot_rows(s))
    atomic_trial(s.root / "pilot.json", decision)
    print("pilot", decision, flush=True)


if __name__ == "__main__":
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("command", choices=["prepare", "pilot"])
    args = cli.parse_args()
    cfg = config(Path(".env.glm"))
    if args.command == "prepare":
        proto = prepare(cfg)
        atomic_trial(ROOT / "protocol.json", proto)
        atomic_trial(PUBLIC, proto)
        print("registered", digest(proto), "noise", proto["noise"])
    else:
        with session(read(PUBLIC), cfg) as study:
            pilot(study)
