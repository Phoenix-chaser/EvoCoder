"""Run a registered, outcome-gated GLM pretest and transfer study. All model calls use the original ledger."""
import argparse
import fcntl
import itertools
import re
import threading
import time
from pathlib import Path
from uuid import uuid4

from evocoder.comparison import clone_state, dump, sources
from evocoder.contracts import Settings, canonical, digest
from evocoder.dataset import load_catalog
from evocoder.jobs import bounded_map
from evocoder.store import Store
from falsy_transfer_cases import DEV, PILOT, UPSTREAM, make_case
from falsy_transfer_protocol import (
    DEV_REPEATS, FAMILIES, LEARNED, PILOT_REPEATS, PREVIOUS, REFERENCE, calibrate, order, pilot_decision, probe_patch,
)
from report_click_procedure import Archive
from run_optional_control import Study, StudyLibrary, curve_point, read

WORKERS = 2
SCRIPTS = ["run_falsy_transfer.py", "falsy_transfer_cases.py", "falsy_transfer_protocol.py", "run_optional_control.py"]
HERE = Path(__file__).resolve().parent


def require(ok, message):
    if not ok:
        raise ValueError(message)


def code_hashes():
    return {name: digest((HERE / name).read_text()) for name in SCRIPTS} | {"runtime": digest(sources())}


def atomic_trial(path, result):
    if path.is_file():
        require(read(path) == result, "different immutable trial receipt")
    else:
        dump(path, result)


def prepare(root, origin, cache, settings):
    require(not (root / "protocol.json").exists(), "registered protocol already exists; use run to resume")
    previous = read(origin / "protocol.json")
    require(digest(previous) == PREVIOUS and read(origin / "root-proposal.json")["skill"] == LEARNED,
            "expected the original, live, unpromoted learned root")
    prev_manifest = origin / "dataset/manifest.json"
    require(digest(read(prev_manifest)) == previous["manifest"], "changed original holdout manifest")
    cases = [make_case(name, cache) for name in PILOT + DEV]
    calibration = bounded_map(lambda item: calibrate(item, settings), cases, 4)
    original = [t for t in load_catalog(prev_manifest) if t.split in ("learn", "test")]
    all_tasks = [*(t for t, _ in cases), *original]
    directory = root / "dataset"
    directory.mkdir(parents=True, exist_ok=True)
    entries = []
    for task in all_tasks:
        path = task.id + ".json"
        (directory / path).write_text(canonical(task.model_dump()) + "\n")
        entries.append({"id": task.id, "path": path, "hash": digest(task.model_dump()), "source_group": task.source_group,
                        "split": task.split})
    dump(directory / "manifest.json", {"tasks": entries})
    load_catalog(directory / "manifest.json")
    previous_entries = read(prev_manifest)["tasks"]
    protocol = {
        "version": 1, "study": "falsy-boundary-transfer", "instance": uuid4().hex, "codes": code_hashes(),
        "previous": PREVIOUS, "learned": LEARNED, "manual": REFERENCE.model_dump(), "manual_hash": REFERENCE.hash,
        "image": settings.image, "model": settings.model, "settings_hash": digest(settings.public()),
        "cases": entries, "calibration": calibration, "upstream": {k:dict(repository=v[0], base=v[1], fixed=v[2])
                                                                  for k,v in UPSTREAM.items()},
        "pilot": ["transfer-" + n for n in PILOT], "dev": ["transfer-" + n for n in DEV],
        "holdout": {f:[r["id"] for r in previous_entries if r["split"] == "test" and r["family"] == f] for f in FAMILIES},
        "pilot_repeats": PILOT_REPEATS, "dev_repeats": DEV_REPEATS, "workers": WORKERS, "solver_slots": 15,
        "pilot_rule": "All four cases three times. Proceed if at least two same-case repeats have a baseline "
                      "submitted patch passing public and ordinary/default controls but failing the same named falsy "
                      "probe, paired with a fully successful forced manual skill.",
        "transfer_rule": "Use the unchanged model-authored root with semantic routing. Compare all three arms "
                         "on new development cases twice. Release only on the original library gate with full support.",
        "holdout_rule": "After activation only, compare flat-policy baseline and frozen skill in three equal strata "
                        "from the previous still-sealed thirty. No tuning or writeback. Show cumulative and marginal gain.",
        "transport": "Keep all receipts and reservations. At most two fresh trial IDs for a terminal transient "
                     "provider error. Never repeat graded failures, exhausted solvers, or unresolved executions.",
    }
    dump(root / "protocol.json", protocol)
    return protocol


class TransferStudy(Study):
    def solve(self, task, phase, repeat=0, **kwargs):
        for attempt in range(3):
            label = phase if attempt == 0 else f"{phase}-transport-{attempt}"
            try:
                return super().solve(task, label, repeat, **kwargs)
            except RuntimeError as exc:
                msg = str(exc)
                match = re.match(r"execution ([0-9a-f]{32}) stopped in ERROR: (.*)", msg, re.S)
                if not match or not re.search(r"\b(429|50[0234])\b|ReadTimeout|ConnectTimeout|ConnectError|RemoteProtocolError",
                                              match[2], re.I):
                    raise
                atomic_trial(self.root / "transport" / (match[1]+".json"),
                             {"failed_run": match[1], "phase": phase, "task":task.id, "repeat":repeat, "attempt":attempt})
                if attempt == 2:
                    raise
                time.sleep(10 * (attempt + 1))


def initialize(root, origin, cfg, ledger, protocol):
    path = root / "state"
    if not path.exists():
        source = Archive(origin / "state/evocoder.sqlite3")
        require(source.get_artifact(LEARNED)["parent"] is None, "previous skill is not a learned root")
        store = clone_state(source, path, ledger)
    else:
        store = Store(path, ledger=ledger)
    study = TransferStudy(root, cfg, store, ledger, protocol)
    study.oracle_store = Store(root / "manual-state", ledger=ledger)
    study.oracle_key = study.oracle_store.artifact("skill", REFERENCE.model_dump())
    require(study.oracle_key == protocol["manual_hash"] and study.oracle_key != LEARNED, "incorrect manual injection")
    with study.oracle_store.connect(write=True) as db:
        db.execute("INSERT OR IGNORE INTO skills(id,parent,status,evidence_hash) VALUES(?,NULL,?,?)",
                   (study.oracle_key, "CANDIDATE", digest("pretest-only-reference")))
    require(set(StudyLibrary(study).provenance(LEARNED)["support"]) == {"dispatch-01","export-02","dispatch-03","export-04"},
            "learned root lost original support")
    return study


def receipt(study, task, phase, repeat, arm):
    oracle = arm == "manual"
    key = study.solve(task, ("manual-" if oracle else "") + phase, repeat, pool=[LEARNED] if arm == "learned" else None,
                      oracle=oracle)
    st = study.oracle_store if oracle else study.store
    run, events = st.run(key), st.events(key)
    final = run["result"]
    route, = [e["payload"] for e in events if e["kind"] == "SKILL_ROUTE"]
    calls = [e["payload"] for e in events if e["kind"] == "MODEL_RESPONSE"]
    require(run["task_hash"] == digest(task.model_dump()) and run["mode"] == "live"
            and final["image_id"] == study.settings.image and
            calls and any(c.get("expected_function") == "submit_action" for c in calls)
            and all(c.get("provider_model", "").lower() == study.settings.model.lower() for c in calls),
            "unbound solver identity or image")
    require(run["skill_id"] == route["applied_skill_id"] and run["skill_id"] in
            ({"bare": {None}, "manual": {study.oracle_key}, "learned": {None, LEARNED}}[arm]), "wrong exposure")
    steps = sum(e["kind"] == "ACTION" for e in events)
    require(steps <= 15, "solver action budget differs between arms")
    probes = None
    if final["patch"]:
        probes = probe_patch(task, st.get_artifact(final["patch"]), study.settings)
        require(all(probes.values()) == final["passed"], "probe results disagree with independent grade")
    return {"run":key, "patch":final["patch"], "passed":final["passed"], "state":run["state"], "applied":run["skill_id"],
            "route":route["decision"], "steps":steps, "calls":final["cost"]["calls"], "probes":probes}


def trials(study, phase, ids, repetitions):
    def trial(job):
        index, tid, repeat = job
        task = study.tasks[tid]
        sequence = order(phase, index, repeat)
        row = {"task":tid, "snapshot":digest(task.model_dump()), "repeat":repeat, "order":list(sequence)}
        for arm in sequence:
            row[arm] = receipt(study, task, "gate" if phase == "dev" else phase, repeat, arm)
        atomic_trial(study.root / "trials" / f"{phase}-{tid}-{repeat}.json", row)
        print(phase, tid, repeat + 1, *(f"{a}={int(row[a]['passed'])}" for a in sequence), flush=True)
        return row
    jobs = [(i,t,n) for n in range(repetitions) for i,t in enumerate(ids)]
    return bounded_map(trial, jobs, WORKERS)


def run_stages(s, p, result):
    result["pilot"] = trials(s, "pilot", p["pilot"], PILOT_REPEATS)
    result["pilot_gate"] = pilot_decision(result["pilot"], p["pilot"])
    if not result["pilot_gate"]["passed"]:
        result["phase"] = "pilot_rejected"
        return
    print("pilot_confirmed", result["pilot_gate"]["repeated_rescues"], flush=True)
    result["dev"] = trials(s, "dev", p["dev"], DEV_REPEATS)
    lib = StudyLibrary(s)
    gate_path = s.root / "gate.json"
    if gate_path.exists():
        gate = read(gate_path)
    else:
        support = [s.tasks[t] for t in sorted(lib.provenance(LEARNED)["support"])]
        gate = lib.evaluate(LEARNED, [s.tasks[t] for t in p["dev"]], support, workers=WORKERS)
        dump(gate_path, gate)
    require(gate["candidate"] == LEARNED and s.store.get_artifact(gate["receipt"]) ==
            {k:v for k,v in gate.items() if k != "receipt"}, "unbound release receipt")
    lookup = {(r["task"], r["repeat"]):r for r in result["dev"]}
    for row in gate["rows"]:
        if row["split"] == "dev":
            original = lookup[row["task_id"], row["repeat"]]
            require((original["bare"]["run"], original["learned"]["run"]) ==
                    (row["baseline_run"], row["candidate_run"]), "release did not reuse the registered comparison")
    result["gate"] = gate
    if not gate["gate"]["passed"]:
        result["phase"] = "development_rejected"
        return
    if s.store.active_library() is None:
        s.store.activate_library(gate["library_id"], gate["receipt"], expected=None, mode="live")
    require(s.store.active_library() == gate["library_id"], "unexpected release")
    result["published"] = True
    print("release_passed", flush=True)
    stop = threading.Event()
    for family in FAMILIES:
        tasks = [s.tasks[t] for t in p["holdout"][family]]
        for t in tasks:
            s.ledger.claim_test(t.id, digest(t.model_dump()), s.protocol)
        def pair(item):
            i, task = item
            row = lib.compare_pair(task, 0, [], [LEARNED], LEARNED, reverse=bool(i % 2), stop=stop)
            return dict(row, family=family)
        pairs = bounded_map(pair, list(enumerate(tasks)), WORKERS)
        result["holdout"].extend(pairs)
        result["curve"].append(curve_point(result["holdout"], family, pairs))
        dump(s.root / "progress.json", result)
        print("holdout", family, result["curve"][-1], flush=True)
    result["phase"] = "complete"


def execute(root, origin, cfg):
    p = read(root / "protocol.json")
    require(p["codes"] == code_hashes() and p["settings_hash"] == digest(cfg.public())
            and p["learned"] == LEARNED and p["manual_hash"] == REFERENCE.hash
            and p["cases"] == read(root / "dataset/manifest.json")["tasks"], "changed preregistration")
    ledger = Store(cfg.state_dir, unit=cfg.accounting_unit)
    with (ledger.root / "comparison.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if (root / "result.json").exists():
            saved = read(root / "result.json")
            require(saved["protocol"] == digest(p), "wrong result")
            return saved
        s = initialize(root, origin, cfg, ledger, p)
        start = root / "start.json"
        if not start.exists():
            with ledger.connect() as db:
                cursor = db.execute("SELECT coalesce(max(rowid),0) FROM calls").fetchone()[0]
                already = {r[0] for r in db.execute("SELECT task_id FROM test_exposures")}
            require(not already.intersection(itertools.chain.from_iterable(p["holdout"].values())),
                    "prior exposure of registered holdout")
            dump(start, {"protocol":s.protocol, "ledger_cursor":cursor})
        require(read(start)["protocol"] == s.protocol, "shared ledger cursor mismatch")
        result = {"protocol":s.protocol, "phase":"pilot", "dev":[], "gate":None, "published":False, "holdout":[], "curve":[]}
        try:
            run_stages(s, p, result)
            with s.store.connect() as db:
                tests = {r[0] for r in db.execute("SELECT id FROM runs WHERE split='test'")}
                formal = {r[0] for r in db.execute("SELECT id FROM runs WHERE split='dev' AND task_id LIKE 'transfer-%'")}
            with s.oracle_store.connect() as db:
                formal.update(r[0] for r in db.execute("SELECT id FROM runs WHERE split='dev'"))
            admitted = {r[a]["run"] for phase in ("pilot","dev") for r in result[phase] for a in r["order"]}
            recovered = [read(f) for f in sorted((root / "transport").glob("*.json"))]
            require(tests == {r[a + "_run"] for r in result["holdout"] for a in ("baseline","candidate")}
                    | {r["failed_run"] for r in recovered if r["phase"] == "holdout"}, "unreported test attempt")
            require(formal == admitted | {r["failed_run"] for r in recovered if r["task"] in p["pilot"] + p["dev"]},
                    "unexpected development exposure")
            with ledger.connect() as db:
                exposed = {r[0] for r in db.execute("SELECT task_id FROM test_exposures WHERE experiment=?",(s.protocol,))}
            require(exposed == {r["task_id"] for r in result["holdout"]}, "holdout ledger mismatch")
            result.update(holdout_exposures=len(exposed), transport=recovered, completed=True)
        finally:
            keys = {f.stem for f in (root / "slots").glob("*.json")}
            with ledger.connect() as db:
                charged = {r[0] for r in db.execute("SELECT DISTINCT run_id FROM calls WHERE rowid>?",
                                                    (read(start)["ledger_cursor"],))}
            result["costs"] = ledger.costs(sorted(keys & charged))
            result["unassigned"] = len(charged - keys)
            dump(root / "progress.json", result)
        require(result["unassigned"] == 0, "interleaved billing; inspect original ledger")
        dump(root / "result.json", result)
        return result


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("command", choices=["prepare","run"])
    ap.add_argument("--root", type=Path, required=True)
    ap.add_argument("--origin", type=Path, default=Path("experiments/optional-control-001"))
    ap.add_argument("--cache", type=Path, default=Path("work/benchmark-repos"))
    ap.add_argument("--env-file", type=Path, default=Path(".env.glm"))
    args = ap.parse_args()
    cfg = Settings.from_env_file(args.env_file)
    cfg.validate_live()
    require(cfg.provider == "glm" and cfg.mode == "live" and cfg.model.lower() == "glm-5.3" and
            cfg.skill_routing == "semantic" and cfg.max_steps == 16 and cfg.image.startswith("sha256:"), "expected live GLM contract")
    if args.command == "prepare":
        print("frozen", digest(prepare(args.root,args.origin,args.cache,cfg)))
    else:
        summary = execute(args.root,args.origin,cfg)
        print(summary["phase"], summary["costs"])
