"""One preregistered, coverage-informed check per already exposed Bash completion snapshot."""

import argparse
import ast
import fcntl
import time
from pathlib import Path

from continue_route_study import ObserveRateLimit
from evocoder.comparison import dump
from evocoder.contracts import Settings, digest, model_task_view
from evocoder.model import Model
from evocoder.revision_diagnostic import read
from evocoder.runtime import validation
from evocoder.store import Store
from prepare_click_procedure import REPRODUCTION
from repair_click_authored_checks import has_assertion, request_check
from report_click_procedure import Archive
from run_click_procedure import options_for
from verify_authored_cli_checks import ENV, entries, mutant, replay, verdict
from verify_cli_reach import indices, observed

INSTRUCTION = (
    "The recorded CLI calls repeated the state already present in the public example. Write one "
    "independent script that invokes only the missing case_sensitive value through Bash completion "
    "and asserts its actual records against the documented behavior. You have one script. "
    "No test body or expected output is supplied for the missing state."
)


def public_choice(source):
    """Recognize a literal public declaration. Never infer feature state from a reason string."""
    values = {
        k.value.value
        for n in ast.walk(ast.parse(source))
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "Choice"
        for k in n.keywords
        if k.arg == "case_sensitive" and isinstance(k.value, ast.Constant) and type(k.value.value) is bool
    }
    if len(values) != 1:
        raise ValueError("public Choice example must declare exactly one case_sensitive state")
    return values.pop()


def choice_modes(calls):
    modes = set()
    for row in calls:
        info = row["input"]
        env = info.get("kwargs", {}).get("env", {})
        if not isinstance(env, dict) or not any(
            k.endswith("_COMPLETE") and v == "bash_complete" for k, v in env.items()
        ):
            continue
        modes.update(case for _, _, case in info["options"] if type(case) is bool)
    return sorted(modes)


def register(root):
    old = root / "check-repair"
    dest = old / "coverage-feedback"
    if dest.exists():
        raise ValueError("coverage follow-up already registered")
    main, old_plan, earlier = read(root / "result.json"), read(old / "plan.json"), read(old / "result.json")
    tasks = entries(
        Path(main["protocol"]["learn_manifest"]), list(dict.fromkeys(t["task"] for t in earlier["trials"]))
    )
    subjects = []
    for t in earlier["trials"]:
        task, _ = tasks[t["task"]]
        src = task.files[REPRODUCTION]
        public = public_choice(src)
        last = t["attempts"][-1]
        if last.get("exit") != 0 or not last.get("source") or not last.get("raw_execution_matches"):
            continue
        modes = choice_modes(last["original_calls"])
        if modes == [public]:
            subjects.append(
                {
                    "role": t["role"],
                    "run": t["run"],
                    "task": task.id,
                    "patch": t["patch"],
                    "prior_attempt": last["id"],
                    "prior_source": last["source"],
                    "prior_output": last["output"],
                    "public_mode": public,
                    "measured_modes": modes,
                    "missing_mode": not public,
                }
            )
    if not subjects:
        raise ValueError("no duplicate-coverage learning checks")
    sources = {
        name: digest(Path(__file__).with_name(name).read_text())
        for name in (
            Path(__file__).name,
            "repair_click_authored_checks.py",
            "verify_authored_cli_checks.py",
            "verify_cli_reach.py",
        )
    }
    plan = {
        "main": digest(main),
        "earlier": digest(earlier),
        "earlier_plan": digest(old_plan),
        "sources": sources,
        "registered_at": time.time(),
        "subjects": subjects,
        "actions_per_subject": 1,
        "instruction": INSTRUCTION,
        "scope": "Same learning snapshots. Public literal and measured CLI inputs only. "
        "No off-state expected strings, private grade, mutation, code edits, or further retries for a better answer.",
    }
    dump(dest / "plan.json", plan)
    (dest / Path(__file__).name).write_text(Path(__file__).read_text())
    return plan


def run(root, settings):
    base = root / "check-repair"
    dest = base / "coverage-feedback"
    plan, p = read(dest / "plan.json"), read(root / "protocol.json")
    if (
        (dest / "result.json").exists()
        or plan["main"] != digest(read(root / "result.json"))
        or plan["earlier"] != digest(read(base / "result.json"))
        or plan["instruction"] != INSTRUCTION
        or any(digest(Path(__file__).with_name(k).read_text()) != v for k, v in plan["sources"].items())
    ):
        raise ValueError("completed or changed coverage follow-up")
    opts = options_for(settings, p["settings"], True)
    ledger = Store(opts.state_dir, unit=opts.accounting_unit)
    with (ledger.root / "comparison.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        st = Store(dest / "state", ledger=ledger)
        if not (dest / "start.json").exists():
            dump(dest / "start.json", {"plan": digest(plan), "before": ledger.budget()})
        start = read(dest / "start.json")
        if start["plan"] != digest(plan):
            raise ValueError("coverage starting window changed")
        model = Model(opts, st, ObserveRateLimit())
        archive = Archive(root / "seed/evocoder.sqlite3")
        tasks = entries(Path(p["learn_manifest"]), list(dict.fromkeys(s["task"] for s in plan["subjects"])))
        rows = []
        for i, s in enumerate(plan["subjects"]):
            task, _ = tasks[s["task"]]
            if task.split != "learn" or public_choice(task.files[REPRODUCTION]) != s["public_mode"]:
                raise ValueError("changed public learning example")
            rid = digest([digest(plan), i])[:32]
            feedback = {
                "public_case_sensitive": s["public_mode"],
                "executed_case_sensitive": s["measured_modes"],
                "missing_case_sensitive": s["missing_mode"],
                "instruction": INSTRUCTION,
            }
            payload = {
                "task": model_task_view(task.public()),
                "public_example": task.files[REPRODUCTION],
                "earlier_source": s["prior_source"],
                "earlier_output": s["prior_output"],
                "coverage_feedback": feedback,
                "remaining_actions": 1,
            }
            reply = request_check(model, st, rid, payload)
            source = reply["source"]
            item = {
                "id": rid,
                "role": s["role"],
                "task": s["task"],
                "run": s["run"],
                "patch": s["patch"],
                "source": source,
                "source_hash": digest(source),
                "feedback": feedback,
                "caught_disabled": False,
            }
            if source:
                patch = archive.get_artifact(s["patch"])
                raw = validation({**task.files, **patch}, [*ENV, source], opts, "coverage-action")
                item.update(exit=raw["exit_code"], output=raw["output"][:10000], timeout=raw["timeout"])
            rows.append(item)
        dump(dest / "actions.json", rows)
        for s, a in zip(plan["subjects"], rows, strict=True):
            if not a["source"]:
                continue
            task, ref = tasks[s["task"]]
            patch = archive.get_artifact(s["patch"])
            off = mutant(task, ref)
            src = a["source"]
            raw = validation({**task.files, **patch}, [*ENV, src], opts, "coverage-confirm")
            exact = (
                not raw["timeout"]
                and not a["timeout"]
                and (raw["exit_code"], raw["output"][:10000]) == (a["exit"], a["output"])
            )
            original, good, bad = [replay(task, v, src, opts) for v in (patch, ref, off)]
            oa, ob, oc = [observed(task, v, src, opts) for v in (patch, ref, off)]
            hit = indices(oa, ob, oc)
            modes = choice_modes(oa)
            new_state = bool(exact and modes == [s["missing_mode"]] and has_assertion(src))
            strong = (
                new_state
                and bool(hit)
                and verdict(original, good, bad, a["exit"], opts.image) == "detects_disabled_regression"
            )
            a.update(
                original_action=original,
                reference=good,
                off_mutant=bad,
                original_calls=oa,
                reference_calls=ob,
                mutant_calls=oc,
                matched_call_indices=hit,
                raw_execution_matches=exact,
                measured_modes=modes,
                isolated_new_state=new_state,
                caught_disabled=strong,
            )
        ids = [a["id"] for a in rows]
        cost = st.costs(ids)
        after = ledger.budget()
        if abs(after["spent_credits"] - start["before"]["spent_credits"] - cost["cost_credits"]) > 1e-6:
            raise ValueError("coverage follow-up ledger mismatch")
        result = {
            "plan": digest(plan),
            "main": plan["main"],
            "image": opts.image,
            "model": opts.model,
            "rows": rows,
            "costs": cost,
            "before": start["before"],
            "after": after,
            "holdout_tasks": [],
            "registered_skills": 0,
            "used_for_promotion": False,
        }
        dump(dest / "result.json", result)
        return result


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("command", choices=["register", "run"])
    ap.add_argument("--root", type=Path, required=True)
    ap.add_argument("--env-file", type=Path, default=Path(".env.glm"))
    args = ap.parse_args()
    if args.command == "register":
        plan = register(args.root.resolve())
        print([(s["role"], s["measured_modes"], s["missing_mode"]) for s in plan["subjects"]])
    else:
        result = run(args.root.resolve(), Settings.from_env_file(args.env_file))
        print(
            [(a["role"], a.get("measured_modes"), a["caught_disabled"]) for a in result["rows"]],
            result["costs"],
        )
