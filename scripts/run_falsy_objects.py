"""Conditional caller-owned-object transfer panel. The first study's release rows remain binding."""
import argparse
import copy
import fcntl
import threading
from pathlib import Path

from evocoder.comparison import clone_state, dump
from evocoder.contracts import Settings, canonical, digest
from evocoder.dataset import load_catalog
from evocoder.gate import gate_summary
from evocoder.jobs import bounded_map
from evocoder.store import Store
from falsy_object_cases import CASES, build
from falsy_transfer_protocol import FAMILIES, LEARNED, REFERENCE, probe_patch
from report_click_procedure import Archive, read
from run_falsy_transfer import TransferStudy, atomic_trial, code_hashes, receipt, require
from run_optional_control import StudyLibrary, curve_point

ARMS = ("bare","manual","learned","forced_learned")
HERE = Path(__file__).parent


def sequence(index, repeat):
    rotated = ARMS[index:] + ARMS[:index]
    return list(reversed(rotated)) if repeat else list(rotated)


def extra_hashes():
    return code_hashes() | {n:digest((HERE/n).read_text()) for n in ("falsy_object_cases.py","run_falsy_objects.py")}


def prepare(root, primary, cfg):
    require(not (root/"protocol.json").is_file(),"panel already registered")
    p = read(primary/"protocol.json")
    require(p["settings_hash"] == digest(cfg.public()) and p["learned"] == LEARNED, "different primary settings")
    tasks = load_catalog(primary/"dataset/manifest.json")
    new = [build(n) for n in CASES]
    def calibrate(item):
        task,ref = item
        broken, fixed = [probe_patch(task,patch,cfg) for patch in ({},ref)]
        require(not broken["public"] and broken["controls"] and not all(broken.values()) and all(fixed.values()),
                "uncalibrated object case")
        return {"task":task.id,"snapshot":digest(task.model_dump()),"reference_patch":digest(ref),"broken":broken,"fixed":fixed}
    cal = bounded_map(calibrate,new,3)
    directory = root/"dataset"
    directory.mkdir(exist_ok=True,parents=True)
    entries = []
    for task in tasks + [t for t,_ in new]:
        filename = task.id+".json"
        (directory/filename).write_text(canonical(task.model_dump())+"\n")
        entries.append({"id":task.id,"hash":digest(task.model_dump()),"path":filename})
    dump(directory/"manifest.json",{"tasks":entries})
    p2 = {"version":1,"study":"caller-owned-object-transfer","primary":digest(p),"codes":extra_hashes(),
          "model":cfg.model,"image":cfg.image,"settings_hash":p["settings_hash"],"learned":LEARNED,
          "manual_hash":REFERENCE.hash,"manifest_hash":digest({"tasks":entries}), "calibration":cal,
          "dev":[t.id for t,_ in new], "previous_dev":p["dev"], "holdout":p["holdout"],"arms":list(ARMS),
          "repeats":2, "solver_slots":15,"workers":2,
          "entry":"Only after the complete first study found repeated manual corrections on a caller-owned-object "
                  "pretest and did not release. Do not repeat any completed primary development or support trial.",
          "design":"Same task and snapshot in four counterbalanced arms. Forced application of the identical learned "
                   "skill diagnoses routing only. Release uses normal routing versus unassisted repairs.",
          "release":"Standard two-repeat, two-group gate must pass both on the three new groups and on the union of "
                    "old and new development with original support. Any observed regression remains binding.",
          "holdout_rule":"Only after activation of the unchanged skill. Use the original thirty cases once. "
                         "This is the last development panel in this study."}
    dump(root/"protocol.json",p2)
    return p2


class CombinedLibrary(StudyLibrary):
    def __init__(self, study, old):
        super().__init__(study)
        self.old = {(r["task_id"],r["repeat"]):r for r in old["rows"]}

    def compare_pair(self, task, repeat, baseline, variants, candidate, *, reverse, stop, fixed_route=None):
        if (task.id,repeat) in self.old:
            row = self.old[task.id,repeat]
            require(row["task_hash"] == digest(task.model_dump()) and not baseline and variants == [candidate] == [LEARNED],
                    "old row does not match the current release comparison")
            for arm in ("baseline","candidate"):
                run = self.store.run(row[arm+"_run"])
                require(run["task_hash"] == row["task_hash"] and run["skill_id"] == row[arm+"_applied_skill"] and
                        run["result"]["passed"] == row[arm+"_pass"], "replayed previous row differs from archive")
            return dict(row)
        return super().compare_pair(task,repeat,baseline,variants,candidate,reverse=reverse,stop=stop,fixed_route=fixed_route)


def stages(study, p, primary, result):
    require(study.oracle_key == REFERENCE.hash, "changed manual procedure")
    # The direct-skill route in the original solver takes an explicit skill and a store. Bind
    # it to the actual learned artifact in a separate view; no human-authored text enters this arm.
    forced = copy.copy(study)
    forced.oracle_key, forced.oracle_store = LEARNED, study.store
    def trial(job):
        index,tid,repeat = job
        task = study.tasks[tid]
        row = {"task":tid,"snapshot":digest(task.model_dump()),"repeat":repeat,"order":sequence(index,repeat)}
        for arm in row["order"]:
            if arm == "forced_learned":
                row[arm] = receipt(forced,task,"object-fixed",repeat,"manual")
                require(row[arm]["applied"] == LEARNED and row[arm]["route"] == "forced", "learned text not applied")
            else:
                row[arm] = receipt(study,task,"gate",repeat,arm)
        atomic_trial(study.root/"trials"/f"{tid}-{repeat}.json",row)
        print(tid,repeat+1,*(f"{a}={int(row[a]['passed'])}" for a in row["order"]),flush=True)
        return row
    jobs = [(i,t,n) for n in range(2) for i,t in enumerate(p["dev"])]
    result["dev"] = bounded_map(trial,jobs,2)
    lib = CombinedLibrary(study,primary["gate"])
    support = [study.tasks[t] for t in sorted(lib.provenance(LEARNED)["support"])]
    gate_file = study.root/"gate.json"
    if not gate_file.exists():
        gate = lib.evaluate(LEARNED,[study.tasks[t] for t in p["previous_dev"]+p["dev"]],support,workers=2)
        dump(gate_file,gate)
    full = read(gate_file)
    require(full["candidate"] == LEARNED and study.store.get_artifact(full["receipt"]) ==
            {k:v for k,v in full.items() if k!="receipt"},"unbound combined gate")
    added = [r for r in full["rows"] if r["task_id"] in p["dev"]]
    lookup = {(r["task"],r["repeat"]):r for r in result["dev"]}
    for r in added:
        pair = lookup[r["task_id"],r["repeat"]]
        require((r["baseline_run"],r["candidate_run"]) == (pair["bare"]["run"],pair["learned"]["run"]),
                "diagnostic arm entered release decision")
    result.update(gate=full,added_decision=gate_summary(added,2))
    if not (full["gate"]["passed"] and result["added_decision"]["gate"]["passed"]):
        result["phase"] = "object_gate_rejected"
        return
    if study.store.active_library() is None:
        study.store.activate_library(full["library_id"],full["receipt"],expected=None,mode="live")
    require(study.store.active_library() == full["library_id"], "wrong active library")
    result["published"] = True
    stop = threading.Event()
    for family in FAMILIES:
        tasks = [study.tasks[t] for t in p["holdout"][family]]
        for t in tasks:
            study.ledger.claim_test(t.id,digest(t.model_dump()),study.protocol)
        def pair(item):
            i,t = item
            row = lib.compare_pair(t,0,[],[LEARNED],LEARNED,reverse=bool(i%2),stop=stop)
            return dict(row,family=family)
        rows = bounded_map(pair,list(enumerate(tasks)),2)
        result["holdout"].extend(rows)
        result["curve"].append(curve_point(result["holdout"],family,rows))
        dump(study.root/"progress.json",result)
    result["phase"] = "complete"


def execute(root, primary_dir, cfg):
    p, original, first = read(root/"protocol.json"),read(primary_dir/"protocol.json"),read(primary_dir/"result.json")
    require(digest(original) == p["primary"] == first["protocol"] and first["completed"] and
            p["codes"] == extra_hashes() and p["settings_hash"] == digest(cfg.public()) and
            p["manifest_hash"] == digest(read(root/"dataset/manifest.json")),"changed registered study")
    confirmed = set(first["pilot_gate"]["repeated_rescues"])
    require(first["phase"] == "development_rejected" and not first["published"] and first["holdout_exposures"] == 0
            and confirmed.intersection({"transfer-cardinality","transfer-miss-handler","transfer-size-hook"}),
            "object-panel entry condition not observed")
    ledger = Store(cfg.state_dir,unit=cfg.accounting_unit)
    with (ledger.root/"comparison.lock").open("a") as lock:
        fcntl.flock(lock,fcntl.LOCK_EX | fcntl.LOCK_NB)
        finished = root/"result.json"
        if finished.exists():
            result = read(finished)
            require(result["protocol"] == digest(p), "wrong completed object study")
            return result
        start = root/"start.json"
        if not start.exists():
            with ledger.connect() as db:
                cursor = db.execute("SELECT coalesce(max(rowid),0) FROM calls").fetchone()[0]
                exposed = {r[0] for r in db.execute("SELECT task_id FROM test_exposures")}
            require(not exposed.intersection(t for ts in p["holdout"].values() for t in ts),"previously exposed holdout")
            dump(start,{"protocol":digest(p),"cursor":cursor})
        require(read(start)["protocol"] == digest(p),"wrong ledger binding")
        source = Archive(primary_dir/"state/evocoder.sqlite3")
        require(source.get_artifact(first["gate"]["receipt"]) == {k:v for k,v in first["gate"].items() if k!="receipt"},
                "changed primary gate")
        st = Store(root/"state",ledger=ledger) if (root/"state").exists() else clone_state(source,root/"state",ledger)
        s = TransferStudy(root,cfg,st,ledger,p)
        s.oracle_store = Store(root/"manual-state",ledger=ledger)
        s.oracle_key = s.oracle_store.artifact("skill",REFERENCE.model_dump())
        with s.oracle_store.connect(write=True) as db:
            db.execute("INSERT OR IGNORE INTO skills(id,parent,status,evidence_hash) VALUES(?,NULL,?,?)",
                       (s.oracle_key,"CANDIDATE",digest("pretest-only-reference")))
        result = {"protocol":s.protocol,"primary_receipt":first["gate"]["receipt"],"phase":"object_dev","published":False,
                  "holdout":[],"curve":[]}
        try:
            stages(s,p,first,result)
            with ledger.connect() as db:
                exposed = {r[0] for r in db.execute("SELECT task_id FROM test_exposures WHERE experiment=?",(s.protocol,))}
            require(exposed == {r["task_id"] for r in result["holdout"]},"unbound holdout exposure")
            result.update(holdout_exposures=len(exposed),transport=[read(f) for f in sorted((root/"transport").glob("*.json"))])
            result["completed"] = True
        finally:
            slots = {f.stem for f in (root/"slots").glob("*.json")}
            with ledger.connect() as db:
                charged = {r[0] for r in db.execute("SELECT DISTINCT run_id FROM calls WHERE rowid>?",(read(start)["cursor"],))}
            result["costs"] = ledger.costs(sorted(slots & charged))
            result["unassigned"] = len(charged-slots)
            dump(root/"progress.json",result)
        require(result["unassigned"] == 0,"unattributed billing")
        dump(finished,result)
        return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command",choices=("prepare","run"))
    parser.add_argument("--root",type=Path,default=Path("experiments/falsy-objects-001"))
    parser.add_argument("--primary",type=Path,default=Path("experiments/falsy-transfer-001"))
    parser.add_argument("--env-file",type=Path,default=Path(".env.glm"))
    args = parser.parse_args()
    cfg = Settings.from_env_file(args.env_file)
    cfg.validate_live()
    if args.command == "prepare":
        print("registered",digest(prepare(args.root,args.primary,cfg)))
    else:
        result = execute(args.root,args.primary,cfg)
        print(result["phase"],result["costs"])
