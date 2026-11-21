"""Preserve the Click learning and holdout snapshots and freeze new development cases."""

import argparse
import json
from pathlib import Path

from evocoder.contracts import digest
from evocoder.dataset import load_catalog
from prepare_click_spacing import build_case, write_bundle

# The prior timeout case is repeated as a regression check, outside improvement credit.
REGRESSION = "pallets-click-0551bf5358"

DEVELOPMENT = [
    {
        "commit": "fd401cd761a2cf15fbb9a54404860b636a5b558d",
        "upstream": "https://github.com/pallets/click/pull/123",
        "split": "dev",
        "group": "source_indent_in_option_help",
        "scope": "direct",
        "issue": "When a decorated option uses a multiline help string indented in the Python "
        "source, the first paragraph can move off the option row and longer source "
        "indentation shifts the displayed description. Keep the first paragraph on its "
        "option row and align later paragraphs with it. Preserve paragraph boundaries and deliberate "
        "indentation inside a paragraph.",
        "reproduction": r'''
            import click
            from click.testing import CliRunner

            def test_reported_layout():
                @click.command()
                @click.option("--input", help="""
                    Read one item.

                    Repeat as needed.
                """)
                def report(input):
                    pass

                result = CliRunner().invoke(report, ["--help"])
                assert result.exit_code == 0, result.output
                lines = result.output.splitlines()
                first = next(line for line in lines if "Read one item." in line)
                second = next(line for line in lines if "Repeat as needed." in line)
                assert first.startswith("  --input TEXT  ")
                assert first.index("Read one item.") == second.index("Repeat as needed.")
        ''',
    },
    {
        "commit": "bb7b2df4d67bcd02a044d21c5ee3c7acb51f5d88",
        "upstream": "https://github.com/pallets/click/commit/bb7b2df4d67bcd02a044d21c5ee3c7acb51f5d88",
        "split": "dev",
        "group": "optional_metavar_on_counted_flag",
        "scope": "direct",
        "issue": "The help row for a repeatable counting flag shows a value placeholder as though "
        "the flag takes an argument. Omit that placeholder and its separator for counting flags. "
        "Keep the displayed names and descriptions for counting and boolean flags, as well as "
        "the placeholder for ordinary value-taking options.",
        "reproduction": '''
            import click
            from click.testing import CliRunner

            def test_reported_layout():
                @click.command()
                @click.option("-v", "--verbose", count=True, help="Verbosity level.")
                @click.option("--name", help="Display name.")
                def report(verbose, name):
                    pass

                result = CliRunner().invoke(report, ["--help"])
                assert result.exit_code == 0, result.output
                count = next(line for line in result.output.splitlines() if "--verbose" in line)
                value = next(line for line in result.output.splitlines() if "--name" in line)
                assert count.strip().split("  ", 1)[0] == "-v, --verbose"
                assert "--name TEXT" in value
        ''',
    },
    {
        "commit": "1a63c21678a0896ba440bc0fda6ef58e3574510a",
        "upstream": "https://github.com/pallets/click/commit/1a63c21678a0896ba440bc0fda6ef58e3574510a",
        "split": "dev",
        "group": "usage_without_arguments",
        "scope": "direct",
        "issue": "When usage arguments do not fit after a long program name, a custom usage "
        "prefix shifts their continuation line incorrectly. Beyond the prefix or current "
        "indent, whichever is wider, indent the wrapped arguments by four extra spaces. "
        "Preserve the layout for arguments that fit on the first line.",
        "remove_obsolete_public_test": "test_wrapping_long_command_name",
        "reproduction": '''
            import click

            def test_reported_layout():
                prefix = "Run the command: "
                formatter = click.HelpFormatter(width=45)
                formatter.write_usage("long-program-name", "FIRST SECOND", prefix=prefix)
                lines = formatter.getvalue().splitlines()
                assert len(lines) == 2
                assert lines[0].rstrip() == prefix + "long-program-name"
                assert lines[1] == " " * (len(prefix) + 4) + "FIRST SECOND"
        ''',
    },
]


def prepare(repo: Path, previous_bundle: Path, output: Path) -> dict:
    cases = []
    for task in load_catalog(previous_bundle / "manifest.json"):
        if task.split in {"learn", "test"} or task.id == REGRESSION:
            control = json.loads((previous_bundle / "controller" / f"{task.id}.json").read_text())
            cases.append((task, control))
    cases.extend(build_case(repo, case) for case in DEVELOPMENT)
    return write_bundle(cases, output)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--previous-bundle", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    manifest = prepare(args.repo, args.previous_bundle, args.output)
    print(f'{len(manifest["tasks"])} tasks frozen: {digest(manifest)}')
