"""After solving, verify whether an in-budget model-authored assertion detects an off-only regression."""
import ast
import json
from pathlib import Path

from evocoder import click_observer
from evocoder.contracts import digest
from evocoder.runtime import validation
from evocoder.switch_feedback import axis_from_public
from prepare_switch_learning import off_mutant
from verify_authored_cli_checks import ENV, MARK, check_source
from verify_cli_reach import REACH, indices


def observed_run(task, patch, source, axis, settings):
    mark = "    output = result.output"
    if REACH.count(mark) != 1:
        raise ValueError("unexpected replay observer")
    hook = (f"    from _switch_measure import modes_for\n    actual = args[1] if len(args)>1 else kwargs.get('args')\n"
            f"    options = dict(kwargs)\n    if len(args)>3: options['env'] = args[3]\n"
            f"    inputs['modes'] = sorted(modes_for({axis!r}, cmd, actual, options, self.env or {{}}, result))\n")
    files = {**task.files, **patch, "_authored_source.py": source,
             "_switch_measure.py": Path(click_observer.__file__).read_text()}
    result = validation(files, [*ENV, REACH.replace(mark, hook + mark)], settings, "switch-replay")
    lines = [s[len(MARK):] for s in result["output"].splitlines() if s.startswith(MARK)]
    if result["timeout"] or result["image_id"] != settings.image or len(lines) != 1:
        return {"termination": "unverified", "calls": [], "exit": result["exit_code"]}
    return {**json.loads(lines[0]), "exit": result["exit_code"]}


def strict_result(original, good, bad, source, raw_matches, live_exit):
    hit = [i for i in indices(original["calls"], good["calls"], bad["calls"])
           if False in original["calls"][i]["input"]["modes"]]
    try:
        assertions = [n for n in ast.walk(ast.parse(source)) if isinstance(n, ast.Assert)]
    except SyntaxError:
        assertions = []
    line = bad.get("line")
    own_assert = bool(line and any(n.lineno <= line <= n.end_lineno for n in assertions))
    return {"off_call_indices": hit, "authored_assertion_line": line if own_assert else None,
            "detected": bool(raw_matches and live_exit == original["exit"] == good["exit"] == 0
                             and original["termination"] == good["termination"] == "ok" and hit
                             and bad["exit"] != 0 and bad["termination"] == "authored_assertion" and own_assert)}


def verify_trial(store, key, task, reference, opts):
    ev, run = store.events(key), store.run(key)
    axis = axis_from_public(task.files, task.public_command, task.issue)
    if not axis or run["task_hash"] != digest(task.model_dump()) or run["config"] != opts.public():
        raise ValueError("trial has no registered public axis or snapshot")
    views = {e["payload"]["model_step"]: e["payload"]["coverage"] for e in ev if e["kind"] == "SWITCH_FEEDBACK"}
    checks, seen = [], set()
    for a, receipt, obs in zip(ev, ev[1:], ev[2:]):
        if (a["kind"] != "ACTION" or receipt["kind"] not in {"COMMAND_SNAPSHOT", "AUTHORED_CLI_CHECK"}
                or obs["kind"] != "OBSERVATION"):
            continue
        action, meta, observation = a["payload"], receipt["payload"], obs["payload"]["observation"]
        source = check_source(action)
        if not source or not meta["patch"] or observation.get("timeout"):
            continue
        step = meta["model_step"]
        if (step not in views or step > opts.max_steps or views[step]["patch"] != meta["patch"]
                or meta["image"] != opts.image or meta["task_hash"] != run["task_hash"]):
            raise ValueError("check did not run inside its registered feedback/budget")
        argv = [*ENV, source] if action["tool"] == "cli_check" else action["argv"]
        if digest(argv) != meta["argv_hash"] or obs["payload"]["action"] != action:
            raise ValueError("model action and its actual invocation differ")
        if action["tool"] == "cli_check" and (store.get_artifact(meta["script"]) != {"source": source}
                or meta["output_hash"] != digest(observation["output"]) or meta["exit_code"] != observation["exit_code"]):
            raise ValueError("unbound archived authored check")
        patch = store.get_artifact(meta["patch"])
        raw = validation({**task.files, **patch}, argv, opts, "switch-raw")
        matches = (raw["image_id"] == opts.image and not raw["timeout"]
                   and raw["exit_code"] == observation["exit_code"] and digest(raw["output"][:10000]) == digest(observation["output"]))
        original, good, bad = [observed_run(task, tree, source, axis, opts)
                               for tree in (patch, reference, off_mutant(task, reference))]
        check = strict_result(original, good, bad, source, matches, observation["exit_code"])
        seen.update(v for row in original["calls"] for v in row["input"]["modes"])
        checks.append({"seq": a["seq"], "step": step, "source": source, "source_hash": digest(source),
                       "patch": meta["patch"], "current_patch_at_finish": meta["patch"] == run["result"]["patch"],
                       "feedback_before": views[step], "raw_matches": matches, "live_exit": observation["exit_code"],
                       "original": original, "reference": good, "off_mutant": bad, **check})
    measured = [e["payload"]["observation"]["switch_observation"] for e in ev if e["kind"] == "OBSERVATION"
                and "switch_observation" in e["payload"]["observation"]]
    return {"run": key, "task": task.id, "axis": axis, "state": run["state"], "passed": run["result"]["passed"],
            "model_steps": run["result"]["model_steps"], "submitted_patch": run["result"]["patch"],
            "route": next(e["payload"]["decision"] for e in ev if e["kind"] == "SKILL_ROUTE"),
            "feedback_steps": sorted(views), "online_observations": measured,
            "authored_off_observed": False in seen, "checks": checks,
            "detected": any(c["detected"] for c in checks),
            "detected_on_submission": any(c["detected"] and c["current_patch_at_finish"] for c in checks)}
