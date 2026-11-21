"""In-band switch observations derived solely from supplied public examples and actual local CLI calls."""
import ast
import json
from pathlib import Path
from uuid import uuid4

from evocoder import click_observer

DESCRIPTIONS = {
    "case_insensitive": "case-insensitive Choice matching (enabled means case_sensitive=False)",
    "completion_case_sensitive": "case-sensitive Choice matching during Bash completion (case_sensitive=True)",
    "allow_from_autoenv": "automatic lookup of a supplied prefixed environment variable for an option",
    "count": "counting enabled versus a non-counting numeric option with explicit bounds on the help page",
    "add_help_option": "automatic help option enabled or disabled on a command producing a usage error",
}
GUIDANCE = (
    "\npublic_switch_coverage reports actual local CliRunner invocations on the CURRENT editable snapshot. "
    "Use test to measure the supplied example. Before finishing, write and execute an independent cli_check "
    "with a behavioral assertion for the disabled switch state, even if public tests already exercised it. "
    "Choose inputs whose result would differ "
    "if the switch were ignored. Merely configuring the switch or printing a result is insufficient. "
    "Do this within remaining_steps, allowing one action to finish. Coverage records executions, not correctness.\n"
)


def axis_from_public(files, argv, issue):
    axes = set()
    paths = [arg.split("::", 1)[0] for arg in argv]
    # A supplied issue reproduction is more specific than the broad compatibility suite in the same command.
    examples = [path for path in paths if Path(path).name == "test_reported_layout.py"]
    for path in examples or paths:
        if not path.endswith(".py") or path not in files:
            continue
        try:
            tree = ast.parse(files[path])
            completion = any(isinstance(n, ast.Constant) and n.value == "bash_complete" for n in ast.walk(tree))
            for n in ast.walk(tree):
                if not isinstance(n, ast.Call):
                    continue
                name = getattr(n.func, "attr", getattr(n.func, "id", None))
                keys = {k.arg for k in n.keywords if isinstance(k.value, ast.Constant) and type(k.value.value) is bool}
                if name == "Choice" and "case_sensitive" in keys:
                    axes.add("completion_case_sensitive" if completion else "case_insensitive")
                if name in ("option", "Option"):
                    axes.update(keys & {"allow_from_autoenv", "count"})
                if name in ("command", "Command") and ("add_help_option" in keys or
                        ("usage error" in issue.lower() and "help is disabled" in issue.lower())):
                    axes.add("add_help_option")
        except SyntaxError:
            continue
    return next(iter(axes)) if len(axes) == 1 else None


def observed_command(box, argv, axis):
    """Keep raw stdout and exit code. Collect a separate bounded side channel in the same sandbox."""
    directory = "/tmp/evocoder-switch-" + uuid4().hex
    hook = Path(click_observer.__file__).read_text() + f"\ninstall({axis!r}, {directory + '/calls'!r})\n"
    setup = ("from pathlib import Path; import sys; d=Path(sys.argv[1]); d.mkdir(); "
             "(d/'sitecustomize.py').write_text(sys.argv[2]); (d/'calls').touch()")
    if box.execute(["python", "-I", "-c", setup, directory, hook])["exit_code"]:
        raise RuntimeError("switch observer setup failed")
    command = list(argv)
    # Preserve an explicit PYTHONPATH on common env-prefixed Python commands.
    i = 1
    while command[0] == "env" and i < len(command) and "=" in command[i]:
        if command[i].startswith("PYTHONPATH="):
            command[i] = "PYTHONPATH=" + directory + ":" + command[i].split("=", 1)[1]
            break
        i += 1
    else:
        command = ["env", "PYTHONPATH=" + directory + ":/workspace/src:/workspace", *command]
    result = box.execute(command)
    data = box.execute(["head", "-c", "4096", directory + "/calls"])
    modes = set()
    calls = 0
    for line in data["output"].splitlines() if not data["exit_code"] else ():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, list) and all(type(value) is bool for value in row):
            modes.update(row)
            calls += 1
    return {**result, "switch_observation": {"axis": axis, "invocations": calls, "modes": sorted(modes)}}


class SwitchFeedback:
    def __init__(self, axis, patch):
        self.axis, self.patch = axis, patch
        self.observed = {}

    def record(self, patch, origin, observation):
        if observation.get("axis") != self.axis or not patch:
            return
        modes = self.observed.setdefault(patch, {}).setdefault(origin, set())
        modes.update(observation["modes"])

    def view(self):
        records = self.observed.get(self.patch, {})

        def names(values):
            return ["enabled" if state else "disabled" for state in sorted(values)]

        return {"feature": DESCRIPTIONS[self.axis], "patch": self.patch,
                "observed_in_public_test": names(records.get("public", ())),
                "observed_in_own_checks": names(records.get("authored", ())),
                "not_yet_observed": names({True, False}.difference(*records.values()))}
