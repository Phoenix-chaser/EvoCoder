"""Second independent queue for CLI layout learning, pinned before its GLM runs."""

import argparse
from pathlib import Path

from evocoder.contracts import canonical, digest
from prepare_click_procedure import REPRODUCTION, audit, task_case, write_bundle

CASES = [
    {
        "commit": "71f2bafa541e7f798834e74076786ff4281ac83e",
        "group": "sizing_terminal_help_with_extended_color_sequences",
        "functions": ["src/click/_compat.py::_ansi_re"],
        "module_change": "_ansi_re",
        "upstream_tests": [
            "tests/test_formatting.py::test_wrap_text_visible_width",
            "tests/test_compat.py::test_strip_ansi_removes_full_sequence",
            "tests/test_compat.py::test_strip_ansi_leaves_incomplete_sequences",
            "tests/test_compat.py::test_strip_ansi_stops_at_final_byte",
            "tests/test_compat.py::test_term_len",
        ],
        "description": "Extended terminal styling changes wrapping and leaks into plain help",
        "issue": "Commands with styled help descriptions wrap early or split words when a renderer "
                 "uses an extended color control sequence. With color enabled, keep the original styles "
                 "and wrap at the same visible columns as plain text. With color disabled, display the "
                 "same description without escape fragments. Valid CSI controls, including colon-form RGB, "
                 "take zero columns, while adjacent printable text and incomplete controls retain their contents.",
        "public": r'''
            import click
            from click.testing import CliRunner

            START = "\x1b[38:2:30:110:220m"
            END = "\x1b[0m"
            TEXT = "winter mornings stay quiet while the terminal prints this description."

            def render(help_text, color):
                cli = click.Command("weather", help=help_text)
                result = CliRunner().invoke(cli, ["--help"], terminal_width=32, color=color)
                assert result.exit_code == 0, repr(result.exception)
                return result.output

            def test_feature_enabled():
                styled = render(START + TEXT + END, True)
                assert START in styled and END in styled
                assert styled.replace(START, "").replace(END, "") == render(TEXT, True)

            def test_feature_disabled():
                assert render(START + TEXT + END, False) == render(TEXT, False)
        ''',
        "judge": r'''
            import click
            from click.testing import CliRunner

            def test_styled_words_occupy_plain_columns():
                words = ["salt", "stone", "willow", "birch", "maple", "cedar", "oak"]
                opening, closing = "\033[48:2:10:20:30m", "\033[0m"
                plain = " ".join(words)
                fancy = " ".join(opening + w + closing for w in words)
                for width in (24, 37):
                    for color in (True, False):
                        pages = [CliRunner().invoke(click.Command("forest", help=description),
                                 ["--help"], terminal_width=width, color=color) for description in (plain, fancy)]
                        assert all(p.exit_code == 0 for p in pages)
                        styled = pages[1].output
                        assert (opening in styled) is color
                        assert styled.replace(opening, "").replace(closing, "") == pages[0].output
        ''',
        "upstream_regression_added": True,
    },
    {
        "commit": "f316d5cb3aa89b57c14aee54da6ac9062ac5bec4",
        "group": "settling_deferred_terminal_progress_on_exit",
        "functions": ["src/click/_termui_impl.py::render_finish"],
        "upstream_tests": ["tests/test_termui.py::test_progressbar_lands_on_final_position"],
        "description": "Batched terminal progress misses its final remainder",
        "issue": "A batched command-line progress display can finish below its real position. For example, "
                 "if the final partial batch does not reach the update interval, the visible position ends "
                 "below the total. Account for all updates on closing the progress display. The final position "
                 "should be correct after manual and iterator updates, including with display disabled. "
                 "Keep a hidden display silent and preserve the usual position and percent formatting when visible.",
        "public": r'''
            import io
            import click
            from click.testing import CliRunner

            class Terminal(io.StringIO):
                def isatty(self):
                    return True

            def exercise(hidden):
                terminal = Terminal()
                @click.command()
                def collect():
                    with click.progressbar(length=11, update_min_steps=4, file=terminal,
                                          hidden=hidden, show_pos=True, width=6, label="Scanning") as bar:
                        for _ in range(11):
                            bar.update(1)
                    click.echo(f"done={bar.pos}")
                outcome = CliRunner().invoke(collect)
                assert outcome.exit_code == 0, repr(outcome.exception)
                return outcome.output, terminal.getvalue()

            def test_feature_enabled():
                summary, display = exercise(False)
                assert "11/11" in display and summary == "done=11\n"

            def test_feature_disabled():
                summary, display = exercise(True)
                assert display == "" and summary == "done=11\n"
        ''',
        "judge": r'''
            import io
            import click

            class Output(io.StringIO):
                def isatty(self):
                    return True

            def test_large_threshold_keeps_position_and_visibility():
                for hidden in (False, True):
                    for show_position in (False, True):
                        terminal = Output()
                        with click.progressbar(range(6), file=terminal, width=8, update_min_steps=13,
                                               show_pos=show_position, hidden=hidden) as bar:
                            assert list(bar) == list(range(6))
                        assert bar.pos == 6 and bar.finished
                        output = terminal.getvalue()
                        if hidden:
                            assert output == ""
                        elif show_position:
                            assert "6/6" in output
                        else:
                            assert "100%" in output and "6/6" not in output
        ''',
        "upstream_regression_added": True,
    },
]


def prepare(repo, experiments, output, catalog):
    if output.exists():
        raise ValueError("learning bundle destination must be new")
    cases = [task_case(repo, case) for case in CASES]
    receipt = audit(cases, experiments, output, catalog)
    manifest = write_bundle(cases, output)
    manifest.update(family="Independent Click CLI rendering procedure learning", seed_task_id=None,
                    public_nodes=[REPRODUCTION + "::test_feature_" + switch for switch in ("enabled", "disabled")])
    (output / "manifest.json").write_text(canonical(manifest) + "\n")
    (output / "independence.json").write_text(canonical({**receipt, "manifest_hash": digest(manifest)}) + "\n")
    return manifest


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path("work/upstream/click.git"))
    parser.add_argument("--experiments", type=Path, default=Path("experiments"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--legacy-catalog", type=Path, default=Path("benchmarks/realworld/cases.json"))
    args = parser.parse_args()
    print(digest(prepare(args.repo, args.experiments, args.output, args.legacy_catalog)))
