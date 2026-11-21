"""Freeze the Click development tasks for the evidence-grounded revision study."""

import argparse
import json
from pathlib import Path

from evocoder.contracts import digest
from evocoder.dataset import load_catalog
from prepare_click_spacing import build_case, write_bundle


REGRESSIONS = {"pallets-click-fd401cd761", "pallets-click-0551bf5358"}

DEVELOPMENT = [
    {
        "commit": "190b86b37e4c76af638575c81671f670874265a3",
        "upstream": "https://github.com/pallets/click/commit/190b86b37e4c76af638575c81671f670874265a3",
        "split": "dev",
        "group": "parent_arguments_in_command_path",
        "scope": "direct",
        "issue": "A child command's help synopsis leaves out the positional argument fragments "
        "belonging to its parent group. Render the group name, its positional argument metavars "
        "and the subcommand name in invocation order, with one space between adjacent fragments. "
        "Preserve this layout through nested groups, including groups that contribute no argument "
        "fragments. Ordinary parent options should not appear as positional arguments.",
        "reproduction": '''
            import click
            from click.testing import CliRunner

            def test_reported_layout():
                @click.group()
                @click.argument("workspace")
                def tool(workspace):
                    pass

                @tool.command()
                def inspect():
                    pass

                result = CliRunner().invoke(tool, ["builds", "inspect", "--help"])
                assert result.exit_code == 0, result.output
                assert result.output.splitlines()[0] == "Usage: tool WORKSPACE inspect [OPTIONS]"
        ''',
    },
    {
        "commit": "3d029e03eb2a793c64166cb1b690cd63e42461af",
        "upstream": "https://github.com/pallets/click/commit/3d029e03eb2a793c64166cb1b690cd63e42461af",
        "split": "dev",
        "group": "optional_counter_range_in_help_record",
        "scope": "boundary",
        "issue": "A counting flag's help record gains a bracketed range suffix and separator "
        "for its implicit nonnegative counter type. Omit the implicit range annotation while "
        "preserving the help description, any default annotation, and informative explicit "
        "range bounds for other numeric options. Keep the existing spacing when annotations remain.",
        "reproduction": '''
            import click
            from click.testing import CliRunner

            def test_reported_layout():
                @click.command()
                @click.option("-v", count=True, help="Increase detail.")
                def report(v):
                    pass

                result = CliRunner().invoke(report, ["--help"])
                assert result.exit_code == 0, result.output
                line = next(line for line in result.output.splitlines() if "Increase detail." in line)
                assert line.endswith("Increase detail.")
        ''',
    },
    {
        "commit": "42e2821ba3b7b5763f04c4642a83f0b36cd726d5",
        "upstream": "https://github.com/pallets/click/pull/557",
        "split": "dev",
        "group": "optional_help_hint_in_usage_error",
        "scope": "direct",
        "issue": "On a usage error the output omits the line telling users how to get help. "
        "Place the hint after the usage synopsis, using the current command path and the "
        "configured help option. When help is disabled, omit that optional line. Preserve "
        "the single blank separator before the error message in both layouts.",
        "reproduction": '''
            import click
            from click.testing import CliRunner

            def test_reported_layout():
                @click.command()
                @click.argument("name")
                def inspect(name):
                    pass

                result = CliRunner().invoke(inspect, [])
                assert result.exit_code == 2, result.output
                lines = result.output.splitlines()
                assert lines[1:3] == ['Try "inspect --help" for help.', ""]
                assert lines[3].startswith("Error: ")
        ''',
    },
]


def prepare(repo: Path, previous_bundle: Path, output: Path) -> dict:
    cases = []
    for task in load_catalog(previous_bundle / "manifest.json"):
        if task.split in {"learn", "test"} or task.id in REGRESSIONS:
            control = json.loads((previous_bundle / "controller" / f"{task.id}.json").read_text())
            cases.append((task, control))
    if REGRESSIONS != {task.id for task, _ in cases if task.split == "dev"}:
        raise ValueError("the previous bundle must contain both regression sentinels")
    for case in DEVELOPMENT:
        task, control = build_case(repo, case)
        if task.id == "pallets-click-190b86b37e":
            # This old, unrelated argv encoding test writes to pytest's read-only stdin.encoding.
            # Both new upstream help tests and every other test in the file remain active.
            selection = ["-k", "not test_bytes_args"]
            task = task.model_copy(update={
                "public_command": [*task.public_command, *selection],
                "grader_command": [*task.grader_command, *selection],
            })
            control.update(task_hash=digest(task.model_dump()), excluded_unrelated_tests=["test_bytes_args"])
        cases.append((task, control))
    return write_bundle(cases, output)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--previous-bundle", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    manifest = prepare(args.repo, args.previous_bundle, args.output)
    print(f'{len(manifest["tasks"])} tasks frozen: {digest(manifest)}')
