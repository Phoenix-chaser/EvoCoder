"""Freeze real Click fixes with a single enabled CLI example and private off-state controls."""
import argparse
from pathlib import Path

from evocoder.contracts import canonical, digest
from prepare_click_procedure import REPRODUCTION, audit, task_case, write_bundle

CASES = [
    dict(commit="93ba0075f4881b858b61f89a753c7c10e330027e", group="readline_prompt_color_capability",
         functions=["src/click/termui.py::_readline_prompt"],
         upstream_tests=["tests/test_termui.py::test_prompt_and_confirm_ansi_respects_color"],
         description="Readline prompts preserve color codes when ANSI output is disabled",
         issue="Styled prompts and confirmations should follow the same color policy as ordinary CLI output. "
               "Keep the style when the command runs in color. When color is disabled, the prompts and their "
               "confirmation text should remain readable as plain text. Preserve the input interaction and exit status.",
         public=r'''
            import click
            from click.testing import CliRunner

            def test_feature_enabled():
                @click.command()
                def survey():
                    click.confirm(click.style("Proceed?", fg="green"))
                result = CliRunner().invoke(survey, color=True, input="y\n")
                assert result.exit_code == 0
                assert "\x1b[32mProceed?\x1b[0m [y/N]: y\n" == result.output
         ''',
         judge=r'''
            import click
            from click.testing import CliRunner

            def test_plain_interaction():
                @click.command()
                def survey():
                    click.echo(click.prompt(click.style("City", bold=True)))
                outcome = CliRunner().invoke(survey, color=False, input="York\n")
                assert outcome.exit_code == 0
                assert outcome.output == "City: York\nYork\n"
         ''', upstream_regression_added=True),
    dict(commit="32ae2acd3474a57d2450ea849545b2e1258bf76a", group="shell_choice_completion_normalized_spelling",
         functions=["src/click/types.py::shell_complete"],
         upstream_tests=["tests/test_shell_completion.py::test_choice_case_sensitive"],
         description="Bash choice completions disagree with normalized option spellings",
         issue="Bash option completion should present the same choice names that the option accepts. "
               "Case-sensitive choices preserve normalized spelling including enum member names. With case "
               "sensitivity disabled, return normalized lowercase names and match without regard to case. "
               "Do not change the shell completion record format or include unrelated choices.",
         public=r'''
            from enum import Enum
            import click
            from click.testing import CliRunner

            def test_feature_enabled():
                metal = Enum("Metal", ["Ag", "Au", "Cu"])
                cmd = click.Command("tool", params=[click.Option(["-a"], type=click.Choice(metal, case_sensitive=True))])
                result = CliRunner().invoke(cmd, env={"_TOOL_COMPLETE": "bash_complete",
                                                     "COMP_WORDS": "tool -a A", "COMP_CWORD": "2"})
                assert result.exit_code == 0
                assert result.output == "plain,Ag\nplain,Au\n"
         ''',
         judge=r'''
            import click
            from click.testing import CliRunner

            def test_normalized_bash_records():
                cmd = click.Command("tool", params=[click.Option(["-a"], type=click.Choice(["Red", "rose", "Blue"], case_sensitive=False))])
                result = CliRunner().invoke(cmd, env={"_TOOL_COMPLETE": "bash_complete",
                                                     "COMP_WORDS": "tool -a R", "COMP_CWORD": "2"})
                assert result.exit_code == 0
                assert result.output == "plain,red\nplain,rose\n"
         ''', upstream_regression_added=False),
    dict(commit="2ba2fe0cf80a76e09a6e44c3750034574acd33d0", group="progress_callback_item_matches_yield",
         functions=["src/click/_termui_impl.py::update", "src/click/_termui_impl.py::generator",
                    "src/click/termui.py::progressbar"],
         upstream_tests=["tests/test_termui.py::test_progressbar_item_show_func",
                         "tests/test_termui.py::test_progressbar_update_with_item_show_func"],
         description="Progress item labels lag the item being processed",
         issue="When a CLI progress bar has an item label callback, the label should describe the item currently "
               "being processed, including iterator and explicit updates. With item labels disabled, do not display "
               "a default item value. Preserve normal progress formatting and process each item exactly once.",
         public=r'''
            import click
            import click._termui_impl
            from click.testing import CliRunner

            def test_feature_enabled(monkeypatch):
                monkeypatch.setattr(click._termui_impl, "isatty", lambda _: True)
                @click.command()
                def collect():
                    with click.progressbar(range(3), item_show_func=lambda x: str(x)) as bar:
                        for item in bar:
                            click.echo(f" item {item}")
                result = CliRunner().invoke(collect)
                assert result.exit_code == 0
                lines = [line for line in result.output.splitlines() if " item " in line]
                assert len(lines) == 3
                for index, line in enumerate(lines):
                    assert f"{index}    item {index}" in line
         ''',
         judge=r'''
            import click
            import click._termui_impl
            from click.testing import CliRunner

            def test_no_implicit_item_label(monkeypatch):
                monkeypatch.setattr(click._termui_impl, "isatty", lambda _: True)
                @click.command()
                def collect():
                    with click.progressbar(["cedar", "ash"], width=4, item_show_func=None) as bar:
                        seen = list(bar)
                    click.echo("|".join(seen))
                result = CliRunner().invoke(collect)
                assert result.exit_code == 0
                assert result.output.endswith("cedar|ash\n")
                assert result.output.count("cedar") == result.output.count("ash") == 1
         ''', upstream_regression_added=False),
]


def off_control(task, reference):
    """Mutants are evaluator-only. Enabled stays fixed; each mutation has a separate disabled witness."""
    mutant = dict(reference)
    case = task.id.rsplit("-", 1)[1]
    if case == "93ba0075f4":
        path = "src/click/termui.py"
        mutant[path] = task.files[path]
        return mutant
    specs = {
        "32ae2acd34": ("src/click/types.py",
                       "str_choices = [self.normalize_choice(choice, ctx) for choice in self.choices]",
                       "str_choices = [self.normalize_choice(choice, ctx) if self.case_sensitive else str(choice) for choice in self.choices]"),
        "2ba2fe0cf8": ("src/click/_termui_impl.py", "                info_bits.append(item_info)\n",
                       "                info_bits.append(item_info)\n        elif self.current_item is not None:\n            info_bits.append(str(self.current_item))\n"),
    }
    path, old, new = specs[case]
    if mutant[path].count(old) != 1:
        raise ValueError("off-state control no longer matches frozen source")
    mutant[path] = mutant[path].replace(old, new)
    return mutant


def prepare(repo, experiments, output, catalog):
    if output.exists():
        raise ValueError("bundle destination must be new")
    cases = [task_case(repo, spec) for spec in CASES]
    receipt = audit(cases, experiments, output, catalog)
    manifest = write_bundle(cases, output)
    manifest.update(family="Independent Click learning with only the enabled example supplied", seed_task_id=None,
                    requires_public_reproduction=False, public_nodes=[REPRODUCTION + "::test_feature_enabled"])
    (output / "manifest.json").write_text(canonical(manifest) + "\n")
    (output / "independence.json").write_text(canonical({**receipt, "manifest_hash": digest(manifest)}) + "\n")
    return manifest


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path("work/upstream/click.git"))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(digest(prepare(args.repo, Path("experiments"), args.output, Path("benchmarks/realworld/cases.json"))))
