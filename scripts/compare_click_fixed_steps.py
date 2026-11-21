"""Prespecify a fixed-injection diagnostic and keep it outside the normal promotion gate."""
import argparse
import fcntl
import time
from pathlib import Path
from threading import Event

from evocoder.comparison import dump, frozen_knowledge, sources
from evocoder.contracts import Settings, digest
from evocoder.library import SkillLibrary
from evocoder.revision_diagnostic import copy_closed_seed, read
from evocoder.store import Store
from run_click_procedure import MISSING, TrialRunner, options_for, pick


def register(main):
    protocol = read(main / "protocol.json")
    path = main / "fixed-plan.json"
    if path.exists():
        raise ValueError("fixed diagnostic is already registered")
    if protocol["claim"] != MISSING:
        raise ValueError("different study family")
    slots = read(main / "slots.json") if (main / "slots.json").exists() else {}
    if any(k.split("/")[0] not in ("learning", "learning_replay") for k in slots):
        raise ValueError("register before any focus case has started")
    plan = {"protocol": digest(protocol), "registered_at": time.time(), "driver": digest(Path(__file__).read_text()),
            "cells": [{"task": key, "repeat": rep, "reverse": bool((idx + rep) % 2)}
                      for rep in range(2) for idx, key in enumerate(protocol["focus"])],
            "treatment": "force exactly the requested skill on both arms, equal model and tools; diagnostic only",
            "promotion": False, "holdout": False, "earlier_slots": slots}
    dump(path, plan)
    return plan


def evaluate(main, output, settings):
    result, plan = read(main / "result.json"), read(main / "fixed-plan.json")
    p = result["protocol"]
    if (not result["completed"] or not result["candidate"] or digest(p) != plan["protocol"]
            or digest(Path(__file__).read_text()) != plan["driver"] or digest(sources()) != p["source_hash"]
            or not all(r["task_id"] in p["focus"] for r in result["rows"]) or len(result["rows"]) != 4):
        raise ValueError("incomplete normal scoring or altered fixed-route registration")
    opts = options_for(settings, p["settings"], True)
    ledger = Store(opts.state_dir, unit=opts.accounting_unit)
    spec = {"settings": opts.public(), "normal_result": digest(result), "plan": digest(plan)}
    tasks = pick(Path(p["manifest"]), p["focus"])
    with (ledger.root / "comparison.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if (output / "fixed-result.json").exists():
            raise ValueError("fixed diagnostic already closed")
        if not output.exists():
            st = copy_closed_seed(main, output, ledger)
            dump(output / "start.json", {"spec": spec, "budget_before": ledger.budget()})
        else:
            st = Store(output / "seed", ledger=ledger)
        start = read(output / "start.json")
        if start["spec"] != spec:
            raise ValueError("fixed diagnostic provenance changed")
        parent, child = p["parent"], result["candidate"]
        snapshot = frozen_knowledge(st)
        runner = TrialRunner(opts, st, output, spec)
        lib = SkillLibrary(opts, st, runner)
        rows = []
        for cell in plan["cells"]:
            runner.phase, runner.repeat = "fixed_apply", cell["repeat"]
            row = lib.compare_pair(tasks[cell["task"]], cell["repeat"], [parent], [child], child,
                                   reverse=cell["reverse"], stop=Event(), fixed_route="apply")
            if (row["baseline_applied_skill"], row["candidate_applied_skill"]) != (parent, child) or row["infrastructure_error"]:
                raise ValueError("unscorable fixed pair")
            rows.append(row)
            dump(output / "fixed-progress.json", rows)
        if list(tasks) != p["focus"] or frozen_knowledge(st) != snapshot or st.active_library():
            raise ValueError("unexpected change of scope or skill")
        ids = [a["run_id"] for attempts in runner.slots.values() for a in attempts]
        report = {**spec, "candidate": child, "parent": parent, "rows": rows, "run_ids": ids,
                  "costs": st.costs(ids), "before": start["budget_before"], "after": ledger.budget(),
                  "published": False, "holdout_rows": []}
        if abs(report["after"]["spent_credits"]-report["before"]["spent_credits"]-report["costs"]["cost_credits"]) > 1e-6:
            raise ValueError("fixed diagnostic accounting mismatch")
        dump(output / "fixed-result.json", report)
        return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["register", "run"])
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--env-file", type=Path, default=Path(".env.glm"))
    args = parser.parse_args()
    if args.command == "register":
        print(digest(register(args.root)))
    else:
        if args.output is None:
            parser.error("run requires --output")
        result = evaluate(args.root.resolve(), args.output.resolve(), Settings.from_env_file(args.env_file))
        print({"pairs": len(result["rows"]), "calls": result["costs"]["calls"]})
