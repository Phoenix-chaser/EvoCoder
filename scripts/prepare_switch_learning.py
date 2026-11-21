"""Freeze two independent Click repairs and their evaluator-only disabled controls."""
import argparse
from pathlib import Path

from evocoder.contracts import canonical, digest
from prepare_click_procedure import REPRODUCTION, audit, task_case, write_bundle

CHOICE = "pallets-click-c1b34978f1"
AUTOENV = "pallets-click-8d7f03dac8"
CASES = [
    dict(commit="c1b34978f12e54c45f63a32e3560a66047b29063", group="choice_original_spelling_after_conversion",
         functions=["click/types.py::convert"],
         description="Choice conversion returns a normalized string instead of the declared value",
         issue="With case-insensitive Choice matching enabled, a CLI callback receives a folded spelling "
               "instead of the originally declared choice. Return the declared spelling when a value matches. "
               "Preserve ordinary case-sensitive rejection when insensitive matching is turned off and "
               "retain token normalization configured by the command context.",
         public=r'''
            import click
            from click.testing import CliRunner

            def test_feature_enabled():
                @click.command()
                @click.option("--shade", type=click.Choice(["Ochre", "Teal"], case_sensitive=False))
                def paint(shade):
                    click.echo(shade)
                result = CliRunner().invoke(paint, ["--shade", "tEAl"])
                assert result.exit_code == 0
                assert result.output == "Teal\n"
         ''',
         judge=r'''
            import click
            from click.testing import CliRunner

            def test_strict_choice():
                @click.command()
                @click.option("--stone", type=click.Choice(["Opal"], case_sensitive=True))
                def select(stone):
                    click.echo(stone)
                runner = CliRunner()
                assert runner.invoke(select, ["--stone", "opal"]).exit_code == 2
                exact = runner.invoke(select, ["--stone", "Opal"])
                assert (exact.exit_code, exact.output) == (0, "Opal\n")
         ''',
         upstream_tests=["tests/test_options.py::test_case_insensitive_choice",
                         "tests/test_options.py::test_case_insensitive_choice_returned_exactly",
                         "tests/test_normalization.py::test_choice_normalization"], upstream_regression_added=True),
    dict(commit="8d7f03dac8739afed890af0c0921965786c5e83c", group="empty_automatic_environment_option",
         functions=["src/click/core.py::resolve_envvar_value"],
         description="An empty automatically prefixed environment variable shadows the option default",
         issue="When automatic environment lookup is enabled for a Click option, an empty prefixed environment "
               "variable should act as unset. Preserve lookup of populated variables, the declared fallback, "
               "and the option's ability to disable automatic lookup.",
         public=r'''
            import click
            from click.testing import CliRunner

            def test_feature_enabled():
                @click.command(context_settings={"auto_envvar_prefix": "PAINT"})
                @click.option("--shade", allow_from_autoenv=True, default="ochre")
                def paint(shade):
                    click.echo(shade)
                result = CliRunner().invoke(paint, [], env={"PAINT_SHADE": ""})
                assert result.exit_code == 0
                assert result.output == "ochre\n"
         ''',
         judge=r'''
            import click
            from click.testing import CliRunner

            def test_disabled_automatic_lookup():
                @click.command(context_settings={"auto_envvar_prefix": "STONE"})
                @click.option("--gem", allow_from_autoenv=False, default="agate")
                def choose(gem):
                    click.echo(gem)
                result = CliRunner().invoke(choose, [], env={"STONE_GEM": "opal"})
                assert (result.exit_code, result.output) == (0, "agate\n")
         ''',
         upstream_tests=["tests/test_options.py::test_empty_envvar"], upstream_regression_added=True),
]


def off_mutant(task, reference):
    if task.id == CHOICE:
        path, old, new = "click/types.py", "if not self.case_sensitive:", "if True:"
    elif task.id == AUTOENV:
        path, old, new = "src/click/core.py", "self.allow_from_autoenv\n            and ctx.auto_envvar_prefix", "ctx.auto_envvar_prefix"
    else:
        from verify_authored_cli_checks import mutant
        return mutant(task, reference)
    if reference[path].count(old) != 1:
        raise ValueError("control no longer matches pinned upstream source")
    return {**reference, path: reference[path].replace(old, new)}


def prepare(output):
    cases = [task_case(Path("work/upstream/click.git"), spec) for spec in CASES]
    receipt = audit(cases, Path("experiments"), output, Path("benchmarks/realworld/cases.json"))
    manifest = write_bundle(cases, output)
    manifest.update(family="Independent Click switch feedback learning", seed_task_id=None,
                    requires_public_reproduction=False, public_nodes=[REPRODUCTION + "::test_feature_enabled"])
    (output / "manifest.json").write_text(canonical(manifest) + "\n")
    (output / "independence.json").write_text(canonical({**receipt, "manifest_hash": digest(manifest)}) + "\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    target = parser.parse_args().output
    if target.exists():
        parser.error("destination must be new")
    prepare(target)
