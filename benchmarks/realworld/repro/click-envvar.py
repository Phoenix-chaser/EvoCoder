import click


def test_envvar_sequence_is_formatted_as_individual_names():
    option = click.Option(["--units"], envvar=["WEATHER_UNITS", "UNITS"], show_envvar=True)
    assert option.get_error_hint(click.Context(click.Command("cli"))) == (
        "'--units' (env var: 'WEATHER_UNITS', 'UNITS')"
    )
