"""Register and diagnose independent, paired routing decisions before any panel repair."""
import argparse
import fcntl
from collections import Counter
from contextlib import contextmanager
from pathlib import Path

from evocoder.comparison import clone_state, dump, sources
from evocoder.contracts import Settings, Skill, canonical, digest
from evocoder.dataset import load_catalog
from evocoder.errors import RoutingRejected
from evocoder.jobs import bounded_map
from evocoder.library import SkillLibrary
from evocoder.model import Model
from evocoder.routing import library_task_view, select_library_skill
from evocoder.store import Store
from falsy_transfer_protocol import LEARNED, probe_patch
from portfolio_route_cases import cases
from report_click_procedure import Archive, read
from run_falsy_transfer import atomic_trial, require
from run_object_revision import order

PARENT = LEARNED
CHILD = "152c284b2f238348d80c1f9d2ffc2cb43fcec8fe327f4589e66837170ca63c1e"
POOLS = {"parent": [PARENT], "child": [CHILD], "portfolio": sorted([PARENT, CHILD])}
VIEWS = ("issue", "public_preview")
PUBLIC = Path("benchmarks/falsy_transfer/parallel-routing.json")
PRIOR = Path("docs/results/object-witness.json")
ROOT = Path("experiments/parallel-routing-001")
SEED = Path("experiments/object-witness-001")
HERE = Path(__file__).resolve().parent
WORKERS = 3


def code_hashes():
    return {"runtime": digest(sources()), **{n: digest((HERE / n).read_text()) for n in (
        "portfolio_routing.py", "portfolio_route_cases.py", "object_revision_cases.py",
        "run_optional_control.py", "run_falsy_transfer.py", "falsy_transfer_protocol.py",
    )}}


def prepare(root, seed, cfg):
    require(not (root / "protocol.json").exists(), "existing registration")
    old = read(seed / "protocol.json")
    prior = read(PRIOR)
    previous = read(seed / "result.json")
    require(previous["completed"] and previous["phase"] == prior["phase"] == "gate_rejected"
            and previous["protocol"] == prior["protocol"] == digest(old) and not previous["published"]
            and previous["holdout_exposures"] == 0 and previous["proposal"]["child"] == CHILD
            and old["parent"] == PARENT and old["settings"] == digest(cfg.public()), "changed historical results")
    archive = Archive(seed / "state/evocoder.sqlite3")
    lib = SkillLibrary(cfg, archive, runner=object())
    require(lib.retain_parent([], CHILD) == POOLS["portfolio"] and archive.active_library() is None,
            "wrong initial portfolio")
    support = lib.provenance(CHILD)["support"]
    prior_tasks = load_catalog(seed / "dataset/manifest.json")
    new = cases()
    previous_groups = {t.source_group for t in prior_tasks}
    require(len({t.source_group for _, (t, _) in new} - previous_groups) == len(new)
            and not {t.id for t in prior_tasks} & {t.id for _, (t, _) in new}, "diagnostic task overlap")
    def calibrate(item):
        label, (t, ref) = item
        broken, fixed = [probe_patch(t, patch, cfg) for patch in ({}, ref)]
        require(not broken["public"] and broken["controls"] and all(fixed.values())
                and not any(v for k,v in broken.items() if k not in {"public", "controls"}), "invalid learn task")
        return {"id": t.id, "group": t.source_group, "expected": label, "snapshot": digest(t.model_dump()),
                "reference": digest(ref), "broken": broken, "fixed": fixed}
    learn = bounded_map(calibrate, new, WORKERS)
    ds = root / "dataset"
    ds.mkdir(parents=True, exist_ok=True)
    entries = []
    for t in [*prior_tasks, *(t for _, (t, _) in new)]:
        filename = t.id + ".json"
        (ds / filename).write_text(canonical(t.model_dump()) + "\n")
        entries.append({"id": t.id, "hash": digest(t.model_dump()), "path": filename})
    dump(ds / "manifest.json", {"tasks": entries})
    protocol = {
        "study": "parallel-parent-specialization-v1", "parent": PARENT, "child": CHILD, "parent_of_child": PARENT,
        "model": cfg.model, "settings": digest(cfg.public()), "image": cfg.image, "sources": code_hashes(),
        "prior": digest(prior), "prior_protocol": digest(old), "manifest": digest({"tasks": entries}),
        "learn": learn, "objects": old["objects"], "config": old["config"], "support": support,
        "holdout": old["holdout"], "workers": WORKERS, "repeats": 2, "solver_slots": 15,
        "diagnostic": "Six new learn tasks: two constant configuration defaults, two caller-owned exceptions, two unrelated "
        "or explicitly blank-as-missing controls. Each crosses parent only, child only and both, issue vs bounded "
        "public README/editable source excerpts, two identical-input repeats with reversed job order. Only the "
        "issue, optional public excerpts and immutable applicability/exclusions enter the router. All inputs are "
        "frozen; targets and task groups are evaluator-only. A malformed response is not a semantic skip.",
        "choose": "Using only the 24 portfolio diagnostic decisions, select public_preview if both target families' "
        "correct counts do not decrease, their sum strictly increases, control false applications do not increase "
        "and the number of invalid calls does not increase. Otherwise select issue. Freeze before any panel calls.",
        "dev": "Original three object and four configuration dev tasks, two counterbalanced repeats. Fresh baseline "
        "(bare), original parent/issue and portfolio/frozen view. If preview is selected, add parent-only/identical "
        "preview as a diagnostic arm. Preserve target hashes and one normal routing call (none for bare), fifteen "
        "solver actions per arm. Stable production index order by artifact hash; dispatch is entirely model-selected.",
        "gate": "Compare portfolio to original parent normally routed: at least two independent object sources "
        "with parent fail/portfolio pass and actual child use in both repeats, zero object or config paired losses. "
        "Require the ordinary full-library gate against deployment (empty) on all seven dev tasks and all eight "
        "ancestral learn supports, and no loss on those supports vs original parent. No retuning on development.",
        "test": "Only after all gates and actual publication of the two-member library. Once per original thirty "
        "unseen tasks, no skill vs frozen normal portfolio, no learning. Three equal-sized configuration families, "
        "scalar then layers then batch; per-family and cumulative paired gains. No empirical curve on rejection.",
    }
    dump(root / "protocol.json", protocol)
    return protocol


@contextmanager
def session(root, seed, cfg):
    p = read(root / "protocol.json")
    require(p == read(PUBLIC) and p["settings"] == digest(cfg.public()) and p["sources"] == code_hashes()
            and p["manifest"] == digest(read(root / "dataset/manifest.json")) and p["prior"] == digest(read(PRIOR)),
            "registration, code, settings or evidence changed")
    ledger = Store(cfg.state_dir, unit=cfg.accounting_unit)
    with (ledger.root / "comparison.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if not (root / "start.json").exists():
            with ledger.connect() as db:
                cursor = db.execute("SELECT coalesce(max(rowid),0) FROM calls").fetchone()[0]
                exposed = {r[0] for r in db.execute("SELECT task_id FROM test_exposures")}
            require(not exposed.intersection(t for family in p["holdout"].values() for t in family), "previously opened test")
            dump(root / "start.json", {"protocol": digest(p), "cursor": cursor})
        require(read(root / "start.json")["protocol"] == digest(p), "wrong ledger cursor")
        state = root / "state"
        store = Store(state, ledger=ledger) if state.is_dir() else clone_state(Archive(seed / "state/evocoder.sqlite3"), state, ledger)
        yield p, ledger, store


def selection(rows, targets):
    scores = {}
    for view in VIEWS:
        counts = Counter()
        for row in rows:
            if row["pool"] != "portfolio" or row["view"] != view:
                continue
            label = targets[row["task"]]
            if label == "skip":
                counts["control_applied"] += int(row["selected"] is not None)
            else:
                counts[label] += int(row["selected"] == POOLS[label][0])
            counts["invalid"] += int(row["decision"] == "skip_invalid")
        scores[view] = {k: counts[k] for k in ("parent", "child", "control_applied", "invalid")}
    a, b = (scores[v] for v in VIEWS)
    use = (b["parent"] >= a["parent"] and b["child"] >= a["child"]
           and b["parent"] + b["child"] > a["parent"] + a["child"]
           and b["control_applied"] <= a["control_applied"] and b["invalid"] <= a["invalid"])
    return {"view": "public_preview" if use else "issue", "scores": scores}


def route_trial(root, p, cfg, store, task, repeat, pool, view):
    identity = {"protocol": digest(p), "task": task.id, "snapshot": digest(task.model_dump()),
                "repeat": repeat, "pool": pool, "view": view}
    rid = digest(identity)[:32]
    path = root / "routes" / (rid + ".json")
    if path.exists():
        row = read(path)
        require(row["identity"] == identity, "different diagnostic commitment")
        return row
    require(not store.costs([rid])["calls"], "interrupted paid routing decision; recover its original response")
    skills = {k: Skill.model_validate(store.get_artifact(k)) for k in POOLS[pool]}
    try:
        route = select_library_skill(Model(cfg, store), task, skills, rid, context=view)
    except RoutingRejected as e:
        route = {"decision": "skip_invalid", "reason": str(e), "applied": False}
    events = store.events(rid)
    (response,) = [e["payload"] for e in events if e["kind"] == "MODEL_RESPONSE"]
    require(response["provider_model"].lower() == cfg.model.lower() and response["expected_function"] == "select_library_skill",
            "wrong diagnostic provider or function")
    row = dict(identity, identity=identity, run=rid, selected=route.get("skill_id") if route["applied"] else None,
               decision=route["decision"], reason=route["reason"], quotes=route.get("task_quotes", []),
               conditions=route.get("condition_indices", []), request=response["request_body_hash"],
               task_view=digest(library_task_view(task, view)),
               invalid_fields=[issue["loc"] for e in events if e["kind"] == "MODEL_ARGUMENTS_REJECTED"
                               for issue in e["payload"]["issues"]])
    atomic_trial(path, row)
    return row


def diagnostic(root, p, cfg, store):
    targets = {x["id"]: x["expected"] for x in p["learn"]}
    # Never materialize or pass a dev/test task to the diagnostic router.
    from evocoder.contracts import Task
    def one(item):
        i, tid, n = item
        task = Task.model_validate(read(root / "dataset" / (tid + ".json")))
        variants = tuple((k,v) for k in POOLS for v in VIEWS)
        rows = [route_trial(root, p, cfg, store, task, n, k, v) for k, v in order(variants, i, n)]
        print("learn", tid, n + 1, [(r["view"], r["decision"], str(r["selected"])[:6]) for r in rows if r["pool"]=="portfolio"], flush=True)
        return rows
    rows = [r for group in bounded_map(one, [(i,t,n) for n in range(2) for i,t in enumerate(targets)], WORKERS) for r in group]
    frozen = dict(selection(rows, targets), protocol=digest(p), decisions=digest(rows), count=len(rows))
    atomic_trial(root / "route-freeze.json", frozen)
    return frozen


def config(path):
    cfg = Settings.from_env_file(path)
    cfg.validate_live()
    require(cfg.provider == "glm" and cfg.model.lower() == "glm-5.3" and cfg.mode == "live"
            and cfg.skill_routing == "semantic" and cfg.library_route_context == "issue"
            and cfg.max_steps == 16 and cfg.image.startswith("sha256:"), "wrong GLM configuration")
    return cfg


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["prepare", "diagnose"])
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--seed", type=Path, default=SEED)
    parser.add_argument("--env-file", type=Path, default=Path(".env.glm"))
    args = parser.parse_args()
    settings = config(args.env_file)
    if args.command == "prepare":
        print("registered", digest(prepare(args.root, args.seed, settings)))
    else:
        with session(args.root, args.seed, settings) as (p, ledger, store):
            print("frozen", diagnostic(args.root, p, settings, store), flush=True)
