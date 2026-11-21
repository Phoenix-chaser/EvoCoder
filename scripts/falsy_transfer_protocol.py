"""Frozen arms, deterministic calibration and patch-level diagnosis for the transfer study."""
import json
from collections import Counter

from evocoder.contracts import Skill, digest
from evocoder.runtime import validation
from falsy_transfer_cases import SPECS

LEARNED = "4075c246d85f033248397fd90d1d3e2a68c1d0ed16bd69acc9df79c604b50c9f"
PREVIOUS = "3b1971afa199ed0b58cd96bdb6eac95355c6971a3713e5152bc2fba06e9b80b6"
REFERENCE = Skill(
    name="preserve-supplied-option-across-interfaces", parent=None,
    applicability=["An optional argument or setting has a documented fallback. A supplied value may evaluate to false."],
    steps=["Find the public contract for an absent value and determine which sentinel denotes absence.",
           "Trace an explicit value across all public interfaces sharing the option, including storage and restoration. "
           "Check sibling implementations and branches for empty or overfull inputs when they share the contract.",
           "Use the documented sentinel for fallback. Preserve supplied values and callbacks without testing their "
           "truthiness. When a fallback must be computed, compute it only on the missing path.",
           "Check a legitimate falsy value end to end on another documented path in addition to the public reproducer."],
    verification=["Compare the actual value, type or object identity for absent, ordinary and valid falsy inputs. "
                  "For a stored setting, verify the value after reading it back."],
    exclusions=["A documented API that deliberately treats all empty inputs as missing."],
    evidence_refs=["evaluator-authored-before-pretest"], change_summary="Frozen manual positive control for the pretest.",
)

PILOT_REPEATS = 3
DEV_REPEATS = 2
FAMILIES = ["scalar", "layers", "batch"]
ARMS = {"pilot": ("bare", "manual"), "dev": ("bare", "manual", "learned")}


def order(phase, index, repeat):
    if phase == "pilot":
        return ("bare", "manual") if (index + repeat) % 2 == 0 else ("manual", "bare")
    # Six permutations balanced across tasks and rounds; reverse each task's order in round two.
    sequence = [("bare", "manual", "learned"), ("manual", "learned", "bare"), ("learned", "bare", "manual")]
    base = sequence[index % 3]
    return base if repeat == 0 else tuple(reversed(base))


def probe_patch(task, patch, settings):
    names = ["public_test.py", *(k for k in task.grader_files if k.startswith("probe_"))]
    program = ("import json, subprocess,sys\n"
               f"names = {names!r}\n"
               "print(json.dumps([subprocess.run([sys.executable,'-B',n],stdout=subprocess.DEVNULL,"
               "stderr=subprocess.DEVNULL).returncode for n in names]))\n")
    out = validation({**task.files, **patch, **task.grader_files, "measure.py": program},
                     ["python","measure.py"], settings, task.id + "-probe")
    if out["exit_code"] != 0 or out["timeout"] or out["image_id"] != settings.image:
        raise ValueError("independent patch probe failed to run")
    codes = json.loads(out["output"])
    if len(codes) != len(names) or any(x not in (0, 1) for x in codes):
        raise ValueError("incomplete patch probe")
    return dict(zip(["public", *(n.removeprefix("probe_").removesuffix(".py") for n in names[1:])],
                    [x == 0 for x in codes], strict=True))


def calibrate(item, settings):
    task, reference = item
    baseline = probe_patch(task, {}, settings)
    fixed = probe_patch(task, reference, settings)
    targets = list(SPECS[task.id.removeprefix("transfer-")].falsy)
    if baseline["public"] or all(baseline[k] for k in targets) or not all(fixed.values()):
        raise ValueError("broken/reference calibration failed for " + task.id)
    return {"task": task.id, "snapshot": digest(task.model_dump()), "reference_patch": digest(reference),
            "targets": targets, "broken": baseline, "fixed": fixed}


def rescue_targets(row, skill_arm):
    b, c = row["bare"], row[skill_arm]
    checks = b["probes"]
    if not (b["patch"] and checks and checks["public"] and checks["controls"] and
            not b["passed"] and c["passed"] and c["applied"] and c["probes"] and all(c["probes"].values())):
        return []
    return sorted(k for k in checks if k not in ("public", "controls") and not checks[k])


def pilot_decision(rows, task_ids):
    expected = {(key, r) for key in task_ids for r in range(PILOT_REPEATS)}
    observed = {(row["task"], row["repeat"]) for row in rows}
    if len(rows) != len(expected) or expected != observed:
        raise ValueError("pretest requires all designated paired repetitions")
    confirmed = {}
    for task in task_ids:
        counts = Counter(k for row in rows if row["task"] == task for k in rescue_targets(row, "manual"))
        for k, n in sorted(counts.items()):
            if n >= 2:
                confirmed.setdefault(task, {})[k] = n
    return {"passed": bool(confirmed), "repeated_rescues": confirmed}
