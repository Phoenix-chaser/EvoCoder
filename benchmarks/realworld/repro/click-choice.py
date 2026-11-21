import click
from click.testing import CliRunner


def test_optional_choice_has_one_pair_of_brackets():
    @click.command()
    @click.argument("method", type=click.Choice(["foo", "bar"]), required=False)
    def cli(method):
        pass

    result = CliRunner().invoke(cli, ["--help"])
    assert result.exit_code == 0
    assert "[foo|bar]" in result.output
    assert "[[foo|bar]]" not in result.output
