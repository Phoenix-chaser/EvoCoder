"""Preregister a shape-only routing intervention and a fixed-repeat live repair experiment."""

import argparse
import fcntl
import shutil
from collections import Counter
from contextlib import contextmanager
from pathlib import Path

from evocoder.comparison import clone_state, dump, sources
from evocoder.contracts import Task, canonical, digest
from evocoder.jobs import bounded_map
from evocoder.library import SkillLibrary
from evocoder.repeat_gate import DEV_REPEATS, SUPPORT_REPEATS
from evocoder.store import Store
from falsy_transfer_protocol import probe_patch
from portfolio_routing import CHILD, PARENT, POOLS, config, route_trial
from report_click_procedure import Archive, read
from run_falsy_transfer import atomic_trial, require
from shape_learning_cases import cases

PUBLIC = Path("benchmarks/falsy_transfer/shape-routing.json")
RESULT = Path("docs/results/shape-routing.json")
PREVIOUS = Path("docs/results/parallel-routing.json")
SEED = Path("experiments/parallel-routing-001")
ROOT = Path("experiments/shape-routing-001")
VIEWS = ("issue", "public_shape")
DEV_ARMS = ("bare", "parent", "issue_portfolio", "portfolio")
SUPPORT_ARMS = ("bare", "parent", "portfolio")
WORKERS = 4


def code_hashes():
    bound = (
        "shape_learning_cases.py",
        "shape_study.py",
        "portfolio_routing.py",
        "portfolio_route_cases.py",
        "object_revision_cases.py",
        "run_parallel_routing.py",
        "run_optional_control.py",
        "run_falsy_transfer.py",
        "falsy_transfer_protocol.py",
        "run_object_revision.py",
    )
    return {
        "runtime": digest(sources()),
        **{n: digest(Path(__file__).with_name(n).read_text()) for n in bound},
    }


def arm_order(arms, index, repeat):
    pos = (0, 1, 3, 2) if len(arms) == 4 else range(len(arms))
    return [arms[(i + index + repeat) % len(arms)] for i in pos]


def prepare(root, seed, cfg):
    require(not (root / "protocol.json").exists(), "registration already exists")
    old, before, observed = read(seed / "protocol.json"), read(seed / "result.json"), read(PREVIOUS)
    require(
        before["completed"]
        and before["phase"] == observed["phase"] == "gate_rejected"
        and before["protocol"] == observed["protocol"] == digest(old)
        and not before["published"]
        and not before["holdout_exposures"]
        and before["gate"]["receipt"] == observed["gate"]["receipt"]
        and old["settings"] == digest(cfg.public()),
        "historical experiment differs",
    )
    archive = Archive(seed / "state/evocoder.sqlite3")
    lib = SkillLibrary(cfg, archive, runner=object())
    require(
        archive.active_library() is None
        and lib.retain_parent([], CHILD) == POOLS["portfolio"]
        and lib.provenance(CHILD)["support"] == old["support"],
        "unbound retained skills",
    )
    manifest = read(seed / "dataset/manifest.json")
    require(digest(manifest) == old["manifest"], "different seed task catalog")
    known = {r["id"] for r in manifest["tasks"]}
    ds = root / "dataset"
    shutil.copytree(seed / "dataset", ds, dirs_exist_ok=True)
    # Only split/group metadata is used to check independence; test content is never diagnostic input.
    groups = {read(ds / r["path"])["source_group"] for r in manifest["tasks"]}
    fresh = cases()
    require(
        len({t.source_group for _, (t, _) in fresh} - groups) == len(fresh)
        and not known.intersection(t.id for _, (t, _) in fresh),
        "confirmation source overlap",
    )

    def calibrate(item):
        label, (task, patch) = item
        broken, fixed = [probe_patch(task, fix, cfg) for fix in ({}, patch)]
        require(
            not broken["public"]
            and broken["controls"]
            and all(fixed.values())
            and not any(v for k, v in broken.items() if k not in {"public", "controls"}),
            "invalid calibration",
        )
        return {
            "id": task.id,
            "group": task.source_group,
            "expected": label,
            "snapshot": digest(task.model_dump()),
            "reference": digest(patch),
            "broken": broken,
            "fixed": fixed,
        }

    fresh_meta = bounded_map(calibrate, fresh, WORKERS)
    for _, (task, _) in fresh:
        filename = task.id + ".json"
        (ds / filename).write_text(canonical(task.model_dump()) + "\n")
        manifest["tasks"].append({"id": task.id, "hash": digest(task.model_dump()), "path": filename})
    dump(ds / "manifest.json", manifest)
    p = {k: old[k] for k in ("model", "settings", "image", "objects", "config", "support", "holdout")}
    p.update(
        study="public-test-shapes-and-repeated-repair-v1",
        parent=PARENT,
        child=CHILD,
        prior=digest(observed),
        prior_protocol=digest(old),
        manifest=digest(manifest),
        sources=code_hashes(),
        workers=WORKERS,
        learn={"revisited": old["learn"], "fresh": fresh_meta},
        views=list(VIEWS),
        dev_arms=list(DEV_ARMS),
        support_arms=list(SUPPORT_ARMS),
        dev_rounds=DEV_REPEATS,
        support_rounds=SUPPORT_REPEATS,
        solver_slots=15,
        release_sources=2,
        diagnosis="Two blinded GLM portfolio routes per view on six revisited learn tasks and six new independent "
        "learn sources. Only the issue and, in the intervention, bounded public test AST shapes and filenames. "
        "A cohort meets the routing target if the child is selected on all four exception judgments, parent "
        "choices on config reach at least three of four and at least the concurrent issue count, false "
        "applications on controls are at most one of four and no more than issue, and invalid decisions do "
        "not increase. Report both cohorts. Always run the one frozen intervention on dev regardless of this "
        "mechanism check. No choosing from alternative candidates or interim stopping.",
        comparison="All seven dev cases five times in four fresh arms. Parent is the original parent/issue. "
        "Issue portfolio directly isolates the routing change at fixed skill content. All eight ancestral supports "
        "three times in three arms. Keep all independent solver outcomes. Within each complete arm rotation every "
        "arm occupies each position once. At most one routing call plus fifteen solver actions per arm; bare has "
        "the identical solver allowance. No content or routing changes during evaluation.",
        gate="At least two independent object sources each with >=3 of 5 actual parent-fail/portfolio-pass pairs "
        "using the child with a different first solver request. Count ties in denominator. Across ALL dev and "
        "support tasks, require zero negative changed-input net for each task and no raw loss majority (3/5 on dev, "
        "2/3 on support). Apply the full gate against original parent and current no-skill deployment. Same-input "
        "wins and losses cancel from attribution symmetrically, including when both retain the parent or both skip; "
        "they remain in raw observations and the majority-loss veto. Missing, duplicated, infrastructure-error or "
        "unsettled evidence cannot authorize publication. The prior-portfolio comparison is a separate diagnostic.",
        test="After actual gated publication only: 30 original untouched configuration tasks, one matched bare/"
        "frozen-portfolio pair each, no learning. Scalar, layers and batch fixed order; family and cumulative "
        "paired repair gain. Publish a diversity curve only with all 30 observed pairs. Engineering release "
        "criterion, no population-level significance claim.",
    )
    dump(root / "protocol.json", p)
    return p


@contextmanager
def session(root, seed, cfg):
    p = read(root / "protocol.json")
    require(
        p == read(PUBLIC)
        and p["sources"] == code_hashes()
        and p["settings"] == digest(cfg.public())
        and p["manifest"] == digest(read(root / "dataset/manifest.json"))
        and p["prior"] == digest(read(PREVIOUS)),
        "different public registration, code, settings or catalog",
    )
    ledger = Store(cfg.state_dir, unit=cfg.accounting_unit)
    with (ledger.root / "comparison.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        start = root / "start.json"
        if not start.exists():
            with ledger.connect() as db:
                cursor = db.execute("SELECT coalesce(max(rowid),0) FROM calls").fetchone()[0]
                exposed = {r[0] for r in db.execute("SELECT task_id FROM test_exposures")}
            require(
                not exposed.intersection(t for group in p["holdout"].values() for t in group),
                "previous test exposure",
            )
            dump(start, {"protocol": digest(p), "cursor": cursor})
        require(read(start)["protocol"] == digest(p), "wrong ledger start")
        state = root / "state"
        store = (
            Store(state, ledger=ledger)
            if state.is_dir()
            else clone_state(Archive(seed / "state/evocoder.sqlite3"), state, ledger)
        )
        yield p, ledger, store


def diagnostic_design(p):
    return [
        (cohort, t, n, arm_order(VIEWS, i, n))
        for n in range(2)
        for cohort, items in p["learn"].items()
        for i, t in enumerate(items)
    ]


def diagnostic_summary(p, rows):
    scores = {}
    for cohort, items in p["learn"].items():
        labels = {x["id"]: x["expected"] for x in items}
        score = {}
        for view in VIEWS:
            counts = Counter()
            for r in rows:
                if r["task"] not in labels or r["view"] != view:
                    continue
                counts["invalid"] += r["decision"] == "skip_invalid"
                if labels[r["task"]] == "skip":
                    counts["control_applied"] += r["selected"] is not None
                else:
                    label = labels[r["task"]]
                    counts[label] += r["selected"] == POOLS[label][0]
            score[view] = {key: counts[key] for key in ("child", "parent", "control_applied", "invalid")}
        old, new = score["issue"], score["public_shape"]
        score["met"] = (
            new["child"] == 4
            and new["parent"] >= max(3, old["parent"])
            and new["control_applied"] <= min(1, old["control_applied"])
            and new["invalid"] <= old["invalid"]
        )
        scores[cohort] = score
    return {
        "protocol": digest(p),
        "view": "public_shape",
        "scores": scores,
        "count": len(rows),
        "routes": digest(rows),
    }


def route_receipts(root, p):
    rows = []
    for _, meta, n, views in diagnostic_design(p):
        for v in views:
            identity = {
                "protocol": digest(p),
                "task": meta["id"],
                "snapshot": meta["snapshot"],
                "repeat": n,
                "pool": "portfolio",
                "view": v,
            }
            key = digest(identity)[:32]
            row = read(root / "routes" / (key + ".json"))
            require(row["identity"] == identity and row["run"] == key, "different routing intent")
            rows.append(row)
    require(
        len(rows) == 48 and len(list((root / "routes").glob("*.json"))) == 48, "incomplete routing diagnosis"
    )
    return rows


def diagnose(root, p, cfg, store):
    def probe(job):
        cohort, meta, n, views = job
        task = Task.model_validate(read(root / "dataset" / (meta["id"] + ".json")))
        require(
            task.split == "learn" and digest(task.model_dump()) == meta["snapshot"],
            "non-learning diagnostic input",
        )
        rows = [route_trial(root, p, cfg, store, task, n, "portfolio", v) for v in views]
        print(
            cohort,
            task.id,
            n + 1,
            [(r["view"], str(r["selected"])[:7], r["decision"]) for r in rows],
            flush=True,
        )

    bounded_map(probe, diagnostic_design(p), p["workers"])
    freeze = diagnostic_summary(p, route_receipts(root, p))
    atomic_trial(root / "route-freeze.json", freeze)
    return freeze


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["prepare", "diagnose"])
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--seed", type=Path, default=SEED)
    parser.add_argument("--env-file", type=Path, default=Path(".env.glm"))
    a = parser.parse_args()
    cfg = config(a.env_file)
    if a.command == "prepare":
        print("registered", digest(prepare(a.root, a.seed, cfg)))
    else:
        with session(a.root, a.seed, cfg) as (p, ledger, store):
            print("frozen", diagnose(a.root, p, cfg, store))
