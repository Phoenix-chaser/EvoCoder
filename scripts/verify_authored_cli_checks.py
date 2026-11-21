"""Replay solver-authored CLI checks on immutable snapshots and calibrated off-state controls."""
import argparse
import json
import re
import shlex
from pathlib import Path
from textwrap import dedent

from evocoder.comparison import dump
from evocoder.contracts import Settings, Task, digest, safe_path
from evocoder.revision_diagnostic import read
from evocoder.runtime import validation
from prepare_click_missing_state import off_control
from prepare_click_procedure import GRADER, PYTEST
from probe_click_route_patches import COUNT, HINT, PROBES, extract_probe
from report_click_procedure import Archive

ENV = ["env", "PYTHONPATH=/workspace/src:/workspace", "python", "-c"]
MARK = "EVOCODER_CLI_OBSERVER="
# The observer runs only after scoring. It logs the real local CliRunner invocation while keeping
# the authored code, package and result unchanged. A missing footer is unverified.
OBSERVER = r'''
import hashlib, json, os, sys, traceback
from pathlib import Path
import click
from click.testing import CliRunner
if not Path(click.__file__).resolve().is_relative_to("/workspace"):
    raise RuntimeError("expected the frozen package")
calls = []
invoke = CliRunner.invoke
def watch(self, *args, **kwargs):
    result = invoke(self, *args, **kwargs)
    output = result.output
    if isinstance(output, str):
        output = output.encode()
    calls.append({"exit": result.exit_code, "output_hash": hashlib.sha256(output).hexdigest(),
                  "size": len(output)})
    return result
CliRunner.invoke = watch
kind, line, exception = "ok", None, None
try:
    exec(compile(Path("_authored_source.py").read_text(), "_authored_source.py", "exec"),
         {"__name__": "__main__", "__file__": "_authored_source.py"})
except BaseException as exc:
    kind, exception = "error", type(exc).__name__
    trace = traceback.extract_tb(exc.__traceback__)
    if isinstance(exc, AssertionError) and trace[-1].filename == "_authored_source.py":
        kind, line = "authored_assertion", trace[-1].lineno
    elif isinstance(exc, SystemExit) and exc.code in (0, None):
        kind = "ok"
finally:
    print("\nEVOCODER_CLI_OBSERVER=" + json.dumps({"calls": calls, "termination": kind,
          "line": line, "exception": exception}), file=sys.__stdout__)
raise SystemExit(int(kind != "ok"))
'''


def entries(manifest, ids):
    rows = {e["id"]: e for e in read(manifest)["tasks"]}
    results = {}
    for task_id in ids:
        row = rows[task_id]
        task = Task.model_validate(read(manifest.parent / safe_path(row["path"])))
        control = read(manifest.parent / "controller" / (task_id + ".json"))
        if row["hash"] != digest(task.model_dump()) or control["task_hash"] != row["hash"]:
            raise ValueError("task/reference binding mismatch")
        results[task_id] = (task, control["reference"])
    return results


def mutant(task, reference):
    if task.id not in (COUNT, HINT):
        return off_control(task, reference)
    if task.id == COUNT:
        path, old, new = ("src/click/core.py", "and not (self.count and self.type.min == 0 and self.type.max is None)",
                          "and self.count and not (self.type.min == 0 and self.type.max is None)")
    else:
        path, old, new = ("click/exceptions.py", "self.cmd.get_help_option(self.ctx) is not None):",
                          "self.ctx.help_option_names):")
    if reference[path].count(old) != 1:
        raise ValueError("off-state calibration no longer matches upstream")
    return {**reference, path: reference[path].replace(old, new)}


def calibrate(learn, evaluation, image, *, mutant_for=mutant):
    opts = Settings(_env_file=None, image=image)
    data = entries(learn, [r["id"] for r in read(learn)["tasks"]]) | entries(evaluation, [COUNT, HINT])
    rows = {}
    for key, (task, ref) in data.items():
        altered = mutant_for(task, ref)
        row = {"task": digest(task.model_dump()), "reference": digest(ref), "off_mutant": digest(altered)}
        if task.split == "learn":
            controls = {"enabled": task.public_command, "disabled": [*PYTEST, GRADER]}
            files = {**task.files, GRADER: task.grader_files[GRADER]}
            row["canaries"] = {}
            for k, patch in (("reference", ref), ("off_mutant", altered)):
                row["canaries"][k] = {}
                for name, argv in controls.items():
                    obs = validation({**files, **patch}, argv, opts, "control-" + key[-10:])
                    expected = int(k == "off_mutant" and name == "disabled")
                    if (obs["timeout"] or obs["image_id"] != image or obs["exit_code"] != expected
                            or (expected and not re.search(r"\b1 failed\b", obs["output"]))):
                        raise ValueError(f"failed {key}/{k}/{name} calibration: {obs['output'][-700:]}")
                    row["canaries"][k][name] = {"argv": digest(argv), "exit": expected, "output": digest(obs["output"])}
        else:
            argv = [*ENV, dedent(PROBES[key])]
            outputs = {}
            for k, patch in (("reference", ref), ("off_mutant", altered)):
                obs = validation({**task.files, **patch}, argv, opts, "control-" + key[-10:])
                if obs["timeout"] or obs["image_id"] != image:
                    raise ValueError("wrong calibration sandbox")
                outputs[k] = extract_probe(obs)["cases"]
            diffs = sorted(k for k, v in outputs["reference"].items() if v != outputs["off_mutant"][k])
            expected = ["disabled_help"] if key == HINT else ["default_and_bounds", "explicit_numeric_bounds"]
            if diffs != expected:
                raise ValueError(f"incorrect dev calibration {key}: {diffs}")
            row.update(canaries=outputs, changed_cases=diffs, argv_hash=digest(argv))
        rows[key] = row
    return {"schema": 1, "image": image, "model_calls": 0, "observer_hash": digest(OBSERVER), "tasks": rows,
            "decision": "A verified check must have a recorded model action/execution, replay its status, "
                        "invoke the local CliRunner, pass on reference, and fail at its own Python assertion on "
                        "the validated off-only mutant with a different observed CLI outcome."}


def check_source(action):
    """Only attribute self-contained inputs that survive a clean patch-only replay."""
    if action["tool"] == "cli_check":
        return action["content"]
    argv = action.get("argv", [])
    if not argv:
        return None
    if argv[0] in ("sh", "bash", "/bin/sh", "/bin/bash") and len(argv) == 3 and argv[1] in {"-c", "-lc"}:
        heredoc = re.fullmatch(r"\s*(?:PYTHONPATH=[^\s;]+\s+)?python3?\s+-\s*<<\s*['\"]?(\w+)['\"]?\n(.*)\n\1\s*", argv[2], re.S)
        if heredoc:
            return heredoc[2]
        try:
            argv = shlex.split(argv[2])
        except ValueError:
            return None
    while argv and (argv[0] == "env" or re.fullmatch(r"PYTHON(PATH|UNBUFFERED|DONTWRITEBYTECODE)=\S*", argv[0])):
        argv = argv[1:]
    if len(argv) == 3 and Path(argv[0]).name in ("python", "python3", "python3.12") and argv[1] == "-c":
        return argv[2]
    return None


def replay(task, patch, script, opts):
    argv = [*ENV, OBSERVER]
    obs = validation({**task.files, **patch, "_authored_source.py": script}, argv, opts, "audit-" + task.id[-10:])
    footer = [line[len(MARK):] for line in obs["output"].splitlines() if line.startswith(MARK)]
    detail = json.loads(footer[0]) if len(footer) == 1 else {"termination": "unobserved", "calls": []}
    return {**detail, "exit": obs["exit_code"], "timeout": obs["timeout"], "image": obs["image_id"]}


def verdict(original, ref, off, live_exit, image):
    if any(v["timeout"] or v["image"] != image for v in (original, ref, off)):
        return "unverified"
    if original["exit"] != live_exit or not original["calls"] or not ref["calls"] or not off["calls"]:
        return "unverified"
    same_input_difference = any(a != b for a, b in zip(ref["calls"], off["calls"]))
    if ref["exit"] == 0 and off["termination"] == "authored_assertion" and same_input_difference:
        return "detects_disabled_regression"
    return "exercises_cli_only"


def audit(root):
    p, result = read(root / "protocol.json"), read(root / "result.json")
    if p["claim"] != "missing_cli_state_revision" or p != result["protocol"] or not result["completed"]:
        raise ValueError("expected a closed missing-state study")
    opts = Settings(_env_file=None, image=p["settings"]["image"])
    calibration = read(root / "cli-controls.json")
    if digest(calibration) != p["controls_hash"] or digest(OBSERVER) != calibration["observer_hash"]:
        raise ValueError("changed control protocol")
    tasks = entries(Path(p["learn_manifest"]), p["learn_ids"]) | entries(Path(p["manifest"]), p["focus"])
    archive = Archive(root / "seed/evocoder.sqlite3")
    checks, trials = [], []
    for slot, attempts in read(root / "slots.json").items():
        phase, task_id, rep, skill = slot.split("/")
        if phase not in {"learning", "learning_replay", "focus"}:
            continue
        k = attempts[-1]["run_id"]
        run = archive.run(k)
        task, ref = tasks[task_id]
        altered = mutant(task, ref)
        fixed = calibration["tasks"][task_id]
        if (run["task_hash"] != fixed["task"] or digest(ref) != fixed["reference"]
                or digest(altered) != fixed["off_mutant"] or run["config"] != p["settings"]):
            raise ValueError("changed run or reference control")
        trial = {"phase": phase, "task_id": task_id, "repeat": int(rep), "run_id": k, "skill": skill,
                 "injected": run["skill_id"], "passed": run["result"]["passed"], "authored_actions": 0,
                 "verified_off": False, "unclassified_commands": 0}
        ev = archive.events(k)
        for index, event in enumerate(ev):
            kind, receipt = event["kind"], event["payload"]
            if kind not in {"AUTHORED_CLI_CHECK", "COMMAND_SNAPSHOT"}:
                continue
            before, after = ev[index-1], ev[index+1]
            # A timed-out ordinary command can destroy its sandbox before an observation exists.
            if kind == "COMMAND_SNAPSHOT" and after["kind"] != "OBSERVATION":
                trial["unclassified_commands"] += 1
                continue
            if before["kind"] != "ACTION" or after["kind"] != "OBSERVATION" or after["payload"]["action"] != before["payload"]:
                raise ValueError("action and observation must be paired")
            action, observed = before["payload"], after["payload"]["observation"]
            direct = kind == "AUTHORED_CLI_CHECK"
            argv = [*ENV, action["content"]] if direct else action["argv"]
            if (digest(argv) != receipt["argv_hash"] or receipt["image"] != opts.image
                    or receipt["task_hash"] != fixed["task"] or direct != (action["tool"] == "cli_check")):
                raise ValueError("unbound script invocation")
            if direct and (archive.get_artifact(receipt["script"]) != {"source": action["content"]}
                           or receipt["output_hash"] != digest(observed["output"])
                           or receipt["exit_code"] != observed["exit_code"] or receipt["timeout"] != observed["timeout"]):
                raise ValueError("authored check evidence mismatch")
            script = check_source(action)
            if script is None or not receipt.get("patch"):
                trial["unclassified_commands"] += 1
                continue
            if not direct and "click" not in script.lower():
                continue
            patch = archive.get_artifact(receipt["patch"])
            if set(patch) != set(task.editable):
                raise ValueError("incomplete action-time snapshot")
            row = {"run": k, "seq": event["seq"], "tool": action["tool"], "source": script,
                   "source_hash": digest(script), "patch": receipt["patch"], "live_exit": observed["exit_code"],
                   "live_output_hash": digest(observed["output"]), "live_output": observed["output"][-2200:]}
            for name, code in (("original_action", patch), ("reference", ref), ("off_mutant", altered)):
                row[name] = replay(task, code, script, opts)
            row["verdict"] = verdict(row["original_action"], row["reference"], row["off_mutant"],
                                     row["live_exit"], opts.image)
            checks.append(row)
            trial["authored_actions"] += 1
            trial["verified_off"] |= row["verdict"] == "detects_disabled_regression"
        trials.append(trial)
    return {"schema": 1, "protocol": digest(p), "controls": digest(calibration), "model_calls_in_replay": 0,
            "trials": trials, "checks": checks}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    args = parser.parse_args()
    data = audit(args.root)
    dump(args.root / "authored-checks.json", data)
    print(json.dumps({"runs": len(data["trials"]), "scripts": len(data["checks"]),
                      "accepted": sum(t["verified_off"] for t in data["trials"])}))
