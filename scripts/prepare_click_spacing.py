"""Freeze real Click layout defects with runnable public reproductions."""

import argparse
import ast
import json
from collections.abc import Iterable
from pathlib import Path
from textwrap import dedent

from evocoder.contracts import Task, canonical, digest
from evocoder.dataset import load_catalog
from mine_benchmark import build

SEED_ID = "pallets-click-42ee8a1f60"
REPOSITORY = "pallets/click"
REPRODUCTION = "public_checks/test_reported_layout.py"
GRADER_PROBE = "judge_checks/test_layout_regressions.py"

CASES = [
    {
        "commit": "850bffe0b9db27f52f6f3f0a66eb6df432ce8bcd",
        "upstream": "https://github.com/pallets/click/pull/2369",
        "split": "learn",
        "group": "command_help_optional_content",
        "scope": "direct",
        "issue": "A command docstring that starts with a form feed has no visible long help. "
        "Its --help output should have a single blank line between the usage line and Options, "
        "just as a command with no docstring does. Today it leaves extra blank lines "
        "when the remaining text is entirely behind that marker. Preserve the normal layout "
        "when a description is visible.",
        "reproduction": r'''
            import click
            from click.testing import CliRunner

            def test_reported_layout():
                command = click.Command("report", help="\n    \f\n\n    For maintainers only.")
                result = CliRunner().invoke(command, ["--help"])
                assert result.exit_code == 0, result.output
                assert result.output.splitlines()[:3] == ["Usage: report [OPTIONS]", "", "Options:"]
        ''',
    },
    {
        "commit": "29bacc773e817b2e49a75b6258399dc3e129b031",
        "upstream": "https://github.com/pallets/click/pull/3509",
        "split": "learn",
        "group": "command_help_optional_content",
        "scope": "direct",
        "issue": "Rendering --help for a deprecated command with no description adds one "
        "space before the deprecation label, shifting it to the right of the normal body indent. "
        "An option with empty help can also have an extra leading space in its help record. "
        "Keep the label at the usual position when help is empty or missing, and retain the "
        "space between a real description and its label.",
        "warnings_as_errors": True,
        "reproduction": '''
            import click
            from click.testing import CliRunner

            def test_reported_layout():
                command = click.Command("inspect", help="", deprecated="Use report")
                result = CliRunner().invoke(command, ["--help"])
                assert result.exit_code == 0, result.output
                label = next(line for line in result.output.splitlines() if "(DEPRECATED" in line)
                assert len(label) - len(label.lstrip()) == 2
        ''',
    },
    {
        "commit": "bc21607df91d6c18f8c1054b82a59127cd17f0ea",
        "upstream": "https://github.com/pallets/click/pull/1832",
        "split": "learn",
        "group": "option_rows_after_wrapping",
        "scope": "direct",
        "issue": "Click inserts an extra blank line between option rows when the preceding "
        "description wraps. Changing the terminal width should reflow the description without "
        "inserting a blank separator in the option list. Keep the existing heading and "
        "paragraph spacing.",
        "reproduction": '''
            import click
            from click.testing import CliRunner

            def test_reported_layout():
                command = click.Command("sync", params=[
                    click.Option(["--source"], help="The path to an input file on the server."),
                    click.Option(["--target"], help="Destination."),
                ])
                result = CliRunner().invoke(command, ["--help"], terminal_width=36)
                assert result.exit_code == 0, result.output
                lines = result.output.splitlines()
                index = next(i for i, line in enumerate(lines) if line.lstrip().startswith("--target"))
                assert lines[index - 1].strip()
        ''',
        "discard_old_padding_tests": True,
        "independent_probe": '''
            import click
            import pytest

            @pytest.mark.parametrize("width", [25, 34, 90])
            def test_adjacent_description_rows(width):
                formatter = click.HelpFormatter(width=width)
                formatter.write_dl([
                    ("one", "Read each existing value before merging configuration options."),
                    ("two", "Next action."),
                    ("three", ""),
                ])
                lines = formatter.getvalue().splitlines()
                assert all(line.strip() for line in lines)
                assert lines[0].startswith("one")
                assert lines[-1] == "three"
                assert "two    Next action." in lines
        ''',
    },
    {
        "commit": "0551bf53588ae87f462d336f24f853a156fefe3a",
        "upstream": "https://github.com/pallets/click/pull/3434",
        "split": "dev",
        "group": "usage_without_arguments",
        "scope": "direct",
        "issue": "A command without parameters and with the options placeholder suppressed "
        "can lose its entire usage line or gain a trailing space or blank continuation line "
        "at narrow widths. Show exactly the usage prefix and command name on one line when "
        "the argument fragment is empty. Keep the separator and wrapping when arguments exist.",
        "reproduction": '''
            import click
            from click.testing import CliRunner

            def test_reported_layout():
                command = click.Command("inspect", options_metavar="")
                result = CliRunner().invoke(command, ["--help"])
                assert result.exit_code == 0, result.output
                assert result.output.splitlines()[0] == "Usage: inspect"
        ''',
    },
    {
        "commit": "dcd991d986d43c4f12cc838c4008dfafc015c21d",
        "upstream": "https://github.com/pallets/click/commit/dcd991d986d43c4f12cc838c4008dfafc015c21d",
        "split": "dev",
        "group": "paragraph_width_with_indent",
        "scope": "boundary",
        "issue": "An indented help paragraph breaks too early. A line including its indent "
        "should be able to use the full configured formatter width. Keep the indent on both "
        "lines and preserve wrapping for words that really do not fit.",
        "reproduction": '''
            import click

            def test_reported_layout():
                first_line = "    Update all cached reports"
                formatter = click.HelpFormatter(width=len(first_line))
                formatter.current_indent = 4
                formatter.write_text("Update all cached reports before noon")
                assert formatter.getvalue().splitlines() == [first_line, "    before noon"]
        ''',
    },
    {
        "commit": "244d914364e447175549dab8c2414cdb73d86a3c",
        "upstream": "https://github.com/pallets/click/commit/244d914364e447175549dab8c2414cdb73d86a3c",
        "split": "test",
        "group": "short_help_control_marker",
        "scope": "boundary",
        "issue": "A group's command list leaks the reflow-control character at the beginning "
        "of some automatically generated short descriptions. Show the command's actual text "
        "in the list, even when the long help opts out of paragraph rewrapping.",
        "reproduction": r'''
            import click
            from click.testing import CliRunner

            def test_reported_layout():
                group = click.Group("jobs")
                group.add_command(click.Command("show", help="\b\nShow queued jobs."))
                result = CliRunner().invoke(group, ["--help"])
                assert result.exit_code == 0, result.output
                assert "\b" not in result.output
                assert "Show queued jobs." in result.output
        ''',
    },
    {
        "commit": "7996a89c5a4ce1c451e2e1c9c5a00eb0c844d4ce",
        "upstream": "https://github.com/pallets/click/pull/3902",
        "split": "test",
        "group": "help_wrapping_inside_words",
        "scope": "boundary",
        "issue": "Wrapping a help page can put a line break inside a hyphenated token, "
        "including an option name. A token that will not fit should stay whole, even if "
        "it takes up a line on its own. Preserve content, indentation and ANSI styling "
        "when wrapping between tokens.",
        "reproduction": '''
            from click.formatting import wrap_text

            def test_reported_layout():
                text = "Use --disable-cache to rebuild reports."
                assert wrap_text(text, width=17).split() == text.split()
        ''',
        "public_selection": (
            "test_reported_layout or truncating_docstring or global_show_default "
            "or formatting_custom_type_metavar or command_write_usage_no_args"
        ),
    },
]


def build_case(repo: Path, case: dict) -> tuple[Task, dict]:
    task, control = build(repo, REPOSITORY, case["commit"])
    files = {**task.files, REPRODUCTION: dedent(case["reproduction"]).lstrip()}
    grader = dict(task.grader_files)
    public_command = [*task.public_command, REPRODUCTION]
    grader_command = list(task.grader_command)
    if case.get("discard_old_padding_tests"):
        # The upstream revert removed the assertions specifying the added padding.
        path = "public_checks/test_formatting.py"
        old = files[path]
        start = old.index("def test_formatting_usage_multiline_option_padding(")
        end = old.index("def test_formatting_with_options_metavar_empty(", start)
        removed = old[start:end]
        assert removed.count("def test_") == 2
        files[path] = old[:start] + old[end:]
        grader[GRADER_PROBE] = dedent(case["independent_probe"]).lstrip()
        grader_command.append(GRADER_PROBE)
    if name := case.get("remove_obsolete_public_test"):
        path = "public_checks/test_formatting.py"
        original = files[path]
        node = next(n for n in ast.parse(original).body if isinstance(n, ast.FunctionDef) and n.name == name)
        lines = original.splitlines(keepends=True)
        start = min([node.lineno, *(decorator.lineno for decorator in node.decorator_list)])
        files[path] = "".join(lines[:start - 1] + lines[node.end_lineno:])
    if case.get("warnings_as_errors"):
        # The snapshot omits pyproject.toml, where upstream configures this filter.
        public_command.extend(["-W", "error"])
        grader_command.extend(["-W", "error"])
    if selection := case.get("public_selection"):
        # The new wrapper superseded private _truncate_visible tests and long-word splitting.
        public_command.remove("public_checks/test_compat.py")
        public_command.extend(["-k", selection])
    snapshot = task.model_dump()
    snapshot.update(
        files=files, grader_files=grader, public_command=public_command, grader_command=grader_command,
        issue=case["issue"], split=case["split"], source_group=REPOSITORY + "::" + case["group"],
    )
    task = Task.model_validate(snapshot)
    control.update(
        task_hash=digest(task.model_dump()), split=task.split, upstream=case["upstream"], scope=case["scope"],
        public_reproduction=REPRODUCTION, authored_grader_probe=GRADER_PROBE if GRADER_PROBE in grader else None,
    )
    return task, control


def check_group_boundaries(cases: Iterable[tuple[Task, dict]]) -> None:
    locations = {}
    for task, control in cases:
        for location in control["functions"]:
            old = locations.setdefault(location, task.split)
            if old != task.split:
                raise ValueError("changed function crosses split: " + location)


def prepare(repo: Path, seed_bundle: Path, output: Path) -> dict:
    seed = next(t for t in load_catalog(seed_bundle / "manifest.json") if t.id == SEED_ID)
    seed_controller = json.loads((seed_bundle / "controller" / f"{SEED_ID}.json").read_text())
    cases = [(seed, seed_controller), *(build_case(repo, case) for case in CASES)]
    return write_bundle(cases, output)


def write_bundle(cases: list[tuple[Task, dict]], output: Path) -> dict:
    check_group_boundaries(cases)
    (output / "tasks").mkdir(parents=True)
    (output / "controller").mkdir()
    catalog = []
    for task, control in cases:
        path = f"tasks/{task.id}.json"
        (output / path).write_text(canonical(task.model_dump()) + "\n")
        (output / "controller" / f"{task.id}.json").write_text(canonical(control) + "\n")
        catalog.append({
            "path": path, "hash": digest(task.model_dump()), "id": task.id,
            "split": task.split, "source_group": task.source_group, "repository": task.repository,
        })
    manifest = {
        "schema": 1, "family": "Click help and usage whitespace",
        "seed_task_id": SEED_ID, "tasks": catalog, "requires_public_reproduction": True,
    }
    (output / "manifest.json").write_text(canonical(manifest) + "\n")
    return manifest


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--seed-bundle", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    manifest = prepare(args.repo, args.seed_bundle, args.output)
    print(f'{len(manifest["tasks"])} tasks frozen: {digest(manifest)}')
