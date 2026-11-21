"""Require that the solving snapshot actually reaches the CLI invocation detecting the off-state mutant."""
import argparse
import json
import time
from pathlib import Path

from evocoder.comparison import dump
from evocoder.contracts import Settings, digest
from evocoder.revision_diagnostic import read
from evocoder.runtime import validation
from report_click_procedure import Archive
from verify_authored_cli_checks import ENV, MARK, OBSERVER, entries, mutant

BEFORE = '''    result = invoke(self, *args, **kwargs)'''
IDENTIFY = '''
    def scalar(x):
        if x is None or isinstance(x, (str, int, bool, float)):
            return x
        if isinstance(x, (list, tuple)):
            return [scalar(i) for i in x]
        if isinstance(x, dict):
            return {str(k):scalar(v) for k,v in x.items()}
        return type(x).__name__
    cmd = args[0] if args else kwargs.get("cli")
    inputs = {"stack": [f.lineno for f in traceback.extract_stack() if f.filename == "_authored_source.py"],
              "command": getattr(cmd, "name", None), "add_help": getattr(cmd, "add_help_option", None),
              "options": [(getattr(p,"name",None),getattr(p,"count",None),getattr(p.type,"case_sensitive",None))
                          for p in getattr(cmd, "params", [])],
              "args": scalar(args[1:]), "kwargs": scalar({k:v for k,v in kwargs.items() if k != "cli"})}
'''
if OBSERVER.count(BEFORE) != 1 or OBSERVER.count('calls.append({"exit":') != 1:
    raise ValueError("unexpected upstream observer")
REACH = OBSERVER.replace(BEFORE, IDENTIFY + BEFORE).replace('calls.append({"exit":', 'calls.append({"input": inputs, "exit":')


def register(root):
    p = read(root / "protocol.json")
    slots = read(root / "slots.json") if (root / "slots.json").is_file() else {}
    if any(k.split("/")[0] not in ("learning", "learning_replay") for k in slots) or (root / "reach-plan.json").exists():
        raise ValueError("register once before focus evaluation")
    plan = {"protocol": digest(p), "registered_at": time.time(), "driver": digest(Path(__file__).read_text()),
            "earlier_slots": slots, "criterion": "Original argv must reproduce the recorded exit and stdout hash. "
            "Original editable snapshot, reference and off-mutant must reach the same invocation identified by "
            "authored source lines, ordered arguments and public CLI configuration at a differing output index."}
    dump(root / "reach-plan.json", plan)
    return plan


def observed(task, patch, source, opts):
    o = validation({**task.files, **patch, "_authored_source.py": source}, [*ENV, REACH], opts, "reach-" + task.id[-10:])
    footer = [x[len(MARK):] for x in o["output"].splitlines() if x.startswith(MARK)]
    return json.loads(footer[0]).get("calls", []) if o["image_id"] == opts.image and not o["timeout"] and len(footer) == 1 else []


def indices(original, ref, off):
    return [i for i, (a, b, c) in enumerate(zip(original, ref, off))
            if a["input"] == b["input"] == c["input"] and a["input"]["stack"]
            and (b["exit"], b["output_hash"]) != (c["exit"], c["output_hash"])]


def audit(root, evidence):
    p, plan = read(root / "protocol.json"), read(root / "reach-plan.json")
    if plan["protocol"] != digest(p) or plan["driver"] != digest(Path(__file__).read_text()):
        raise ValueError("changed reach audit registration")
    opts = Settings(_env_file=None, image=p["settings"]["image"])
    st = Archive(root / "seed/evocoder.sqlite3")
    tasks = entries(Path(p["learn_manifest"]), p["learn_ids"]) | entries(Path(p["manifest"]), p["focus"])
    trials = {x["run_id"]: {"task": x["task_id"], "reached_disabled": False, "caught_disabled": False}
              for x in evidence["trials"]}
    rows = []
    for row in evidence["checks"]:
        k = row["run"]
        trial = trials[k]
        task, ref = tasks[trial["task"]]
        # If the mutant makes no observable difference at any recorded call this script cannot qualify.
        if not any(a != b for a,b in zip(row["reference"]["calls"], row["off_mutant"]["calls"])):
            continue
        ev = st.events(k)
        witness, = [previous["payload"] for previous, current in zip(ev, ev[1:]) if current["seq"] == row["seq"]
                    and previous["kind"] == "ACTION" and current["kind"] in {"COMMAND_SNAPSHOT","AUTHORED_CLI_CHECK"}]
        argv = [*ENV, row["source"]] if witness["tool"] == "cli_check" else witness["argv"]
        patch = st.get_artifact(row["patch"])
        actual = validation({**task.files, **patch}, argv, opts, "raw-reach-" + task.id[-10:])
        matched = (actual["image_id"] == opts.image and not actual["timeout"] and actual["exit_code"] == row["live_exit"]
                   and digest(actual["output"][:10000]) == row["live_output_hash"])
        orig, good, bad = [observed(task, x, row["source"], opts) for x in (patch, ref, mutant(task, ref))]
        hit = indices(orig, good, bad)
        reached = bool(matched and hit)
        caught = reached and row["verdict"] == "detects_disabled_regression"
        trial["reached_disabled"] |= reached
        trial["caught_disabled"] |= caught
        rows.append({"run": k, "seq": row["seq"], "raw_execution_matches": matched, "matched_call_indices": hit,
                     "reached_disabled": reached, "caught_disabled": caught,
                     "original": orig, "reference": good, "off_mutant": bad})
    return {"plan": digest(plan), "model_calls": 0, "runs": trials, "checks": rows}


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--root", type=Path, required=True)
    args = ap.parse_args()
    print(digest(register(args.root)))
