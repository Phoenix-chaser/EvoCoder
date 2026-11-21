"""Freeze three public CLI regressions for procedure learning from the local Click mirror."""

import argparse
import io
import json
import tarfile
from pathlib import Path
from textwrap import dedent

from evocoder.contracts import Task, canonical, digest
from mine_benchmark import git
from prepare_click_spacing import REPRODUCTION, REPOSITORY, write_bundle

GRADER = "judge_checks/test_cli_boundary.py"
PYTEST = ["env", "PYTHONPATH=/workspace/src:/workspace", "PYTEST_DISABLE_PLUGIN_AUTOLOAD=1",
          "python", "-m", "pytest", "-c", "/dev/null", "--rootdir=/workspace", "-p", "no:cacheprovider",
          "--basetemp=/tmp/procedure-pytest", "-q"]
CASES = [
    {
        "commit": "79275462d36bf91078acc074ffdd406c9d1d0d89",
        "group": "empty_visible_subcommand_table",
        "functions": ["click/core.py::format_commands"],
        "upstream_tests": ["tests/test_commands.py::test_auto_shorthelp"],
        "description": "Group help rendering when there are zero visible child commands",
        "issue": "A group help page fails to render when its command listing has no visible rows. "
                 "A group with visible children should include the Commands section, with child names and "
                 "short descriptions. When the last child is hidden, or the group is empty, --help should "
                 "still succeed. Omit the empty section and keep the surrounding usage and options layout.",
        "public": '''
            import click
            from click.testing import CliRunner

            def render(hidden):
                @click.group()
                def toolbox():
                    pass
                @toolbox.command(hidden=hidden, short_help="Publish results.")
                def publish():
                    pass
                result = CliRunner().invoke(toolbox, ["--help"])
                assert result.exit_code == 0, repr(result.exception)
                return result.output

            def test_feature_enabled():
                page = render(False)
                assert "Commands:\\n" in page and "publish  Publish results." in page

            def test_feature_disabled():
                page = render(True)
                assert "Usage: toolbox " in page and "--help" in page
                assert "Commands:" not in page and "publish" not in page
        ''',
        "judge": '''
            import click
            from click.testing import CliRunner

            def test_empty_and_mixed_visible_commands():
                @click.group()
                def app():
                    pass
                r = CliRunner()
                empty = r.invoke(app, ["--help"])
                assert empty.exit_code == 0 and "Commands:" not in empty.output
                @app.group()
                def tools():
                    pass
                @tools.command(hidden=True)
                def internal():
                    pass
                nested = r.invoke(app, ["tools", "--help"])
                assert nested.exit_code == 0 and "Commands:" not in nested.output
                @tools.command(short_help="Keep visible.")
                def export():
                    pass
                mixed = r.invoke(app, ["tools", "--help"])
                assert mixed.exit_code == 0
                assert "Commands:\\n" in mixed.output and "export  Keep visible." in mixed.output
                assert "internal" not in mixed.output
        ''',
        "upstream_regression_added": False,
    },
    {
        "commit": "a6e0d2936891030f5092c9869c7391104f6721d9",
        "group": "option_prompt_default_suffix_visibility",
        "functions": ["src/click/core.py::prompt_for_value"],
        "upstream_tests": ["tests/test_termui.py::test_false_show_default_cause_no_default_display_in_prompt"],
        "description": "Interactive option prompt default suffix visibility",
        "issue": "An option that asks for its value on the terminal prints the default-value annotation "
                 "even when its display is explicitly disabled. The prompt should have the optional bracketed "
                 "suffix when display is enabled and omit it when disabled, preserving the prompt separator "
                 "and the actual default accepted on Enter in both cases.",
        "public": '''
            import click
            from click.testing import CliRunner

            def render(enabled):
                @click.command()
                @click.option("--team", prompt="Team", default="ops", show_default=enabled)
                def app(team):
                    click.echo("selected=" + team)
                result = CliRunner().invoke(app, [], input="\\n")
                assert result.exit_code == 0, repr(result.exception)
                return result.output.splitlines()

            def test_feature_enabled():
                lines = render(True)
                assert lines[0].rstrip() == "Team [ops]:" and lines[-1] == "selected=ops"

            def test_feature_disabled():
                lines = render(False)
                assert lines[0].rstrip() == "Team:" and lines[-1] == "selected=ops"
        ''',
        "judge": '''
            import click
            from click.testing import CliRunner

            def test_hide_and_show_with_typed_override():
                for visible in (False, True):
                    @click.command()
                    @click.option("--port", type=int, default=7, prompt="Port", show_default=visible)
                    def request(port):
                        click.echo(str(port))
                    response = CliRunner().invoke(request, [], input="11\\n")
                    assert response.exit_code == 0
                    assert response.output.splitlines() == [
                        ("Port [7]: 11" if visible else "Port: 11"), "11"]
        ''',
        "upstream_regression_added": True,
    },
    {
        "commit": "19fd4d6e18bc9fce451f92f422696b11169faa57",
        "group": "fish_completion_optional_multiline_description",
        "functions": ["src/click/shell_completion.py::format_completion"],
        "module_change": "_SOURCE_FISH",
        "upstream_tests": ["tests/test_shell_completion.py::test_fish_multiline_help_complete",
                           "tests/test_shell_completion.py::test_full_complete"],
        "description": "Fish completion result framing with and without descriptions",
        "issue": "Fish completion output loses record boundaries when an option has a multiline help description. "
                 "For Fish, output one record as three lines: type, value and description. Escape embedded "
                 "newlines as literal backslash-n within fields. Use an underscore in the description line when "
                 "description display is absent. Keep the generated Fish completion script able to consume these "
                 "records and leave the layouts of other shells alone.",
        "public": r'''
            import click
            from click.testing import CliRunner

            def complete(description):
                @click.command("inspect")
                @click.option("--region", help=description)
                def cli(region):
                    pass
                result = CliRunner().invoke(cli, env={
                    "_INSPECT_COMPLETE": "fish_complete", "COMP_WORDS": "inspect --reg", "COMP_CWORD": "--reg"})
                assert result.exit_code == 0, repr(result.exception)
                return result.output

            def test_feature_enabled():
                assert complete("Region name\nExample: east") == "plain\n--region\nRegion name\\nExample: east\n"

            def test_feature_disabled():
                assert complete(None) == "plain\n--region\n_\n"
        ''',
        "judge": r'''
            import click
            from click.testing import CliRunner

            def test_neighbor_records_keep_value_and_description_boundaries():
                @click.command("inventory")
                @click.option("--blue", help="shades\nexamples\nmore")
                @click.option("--blank", help="")
                def inventory(blue, blank):
                    pass
                r = CliRunner().invoke(inventory, env={"_INVENTORY_COMPLETE": "fish_complete",
                                           "COMP_WORDS": "inventory --bl", "COMP_CWORD": "--bl"})
                assert r.exit_code == 0
                assert r.output.splitlines() == [
                    "plain", "--blue", r"shades\nexamples\nmore", "plain", "--blank", "_"]
        ''',
        "upstream_regression_added": True,
    },
]


def snapshot(repo, commit, paths):
    archive = git(repo, "archive", "--format=tar", commit, "--", *paths, binary=True)
    with tarfile.open(fileobj=io.BytesIO(archive)) as contents:
        return {e.name: contents.extractfile(e).read().decode("utf-8") for e in contents
                if e.isfile() and (e.name.endswith(".py") or e.name.startswith("LICENSE"))}


def task_case(repo, spec):
    commit = spec["commit"]
    parent = git(repo, "rev-parse", commit + "^").strip()
    checkout = git(repo, "ls-tree", "--name-only", parent).splitlines()
    prefix = "src/click" if "src" in checkout else "click"
    licenses = [p for p in checkout if p in {"LICENSE", "LICENSE.rst", "LICENSE.txt", "LICENSE.md"}]
    original = snapshot(repo, parent, [prefix, *licenses])
    editable = sorted(k for k in original if k.startswith(prefix + "/"))
    fixed = snapshot(repo, commit, [prefix])
    if set(fixed) != set(editable):
        raise ValueError("source file set differs across the selected fix")
    tests = {x.split("::", 1)[0] for x in spec["upstream_tests"]}
    judge = snapshot(repo, commit, sorted(tests | {"tests/conftest.py"}))
    judge[GRADER] = dedent(spec["judge"]).lstrip()
    task = Task(id="pallets-click-" + commit[:10], repository=REPOSITORY, split="learn",
                issue=spec["issue"], source_group=REPOSITORY + "::" + spec["group"],
                files={**original, REPRODUCTION: dedent(spec["public"]).lstrip()},
                editable=editable, grader_files=judge, public_command=[*PYTEST, "-rA", REPRODUCTION],
                grader_command=[*PYTEST, *spec["upstream_tests"], GRADER])
    changed = [k for k in editable if original[k] != fixed[k]]
    if set(changed) != {v.split("::", 1)[0] for v in spec["functions"]}:
        raise ValueError("source scope does not match local commit")
    patch = git(repo, "diff", "--unified=0", parent, commit, "--", *changed)
    fingerprint = digest([s.strip() for s in patch.splitlines()
                          if s.startswith(("+", "-")) and not s.startswith(("+++", "---"))])
    return task, {"repository": REPOSITORY, "base_commit": parent, "fixed_commit": commit,
                  "upstream": "https://github.com/pallets/click/commit/" + commit, "split": "learn",
                  "reference": fixed, "functions": spec["functions"], "module_change": spec.get("module_change"),
                  "description": spec["description"], "upstream_regression_added": spec["upstream_regression_added"],
                  "authored_grader_probe": GRADER, "upstream_test_nodes": spec["upstream_tests"],
                  "patch_fingerprint": fingerprint, "task_hash": digest(task.model_dump())}


def audit(cases, experiments, output, external):
    prior_ids, prior_tasks, prior_groups, fixes, patches, functions = set(), set(), set(), set(), set(), set()
    manifests = {}
    for path in sorted(experiments.rglob("manifest.json")):
        if path.resolve().is_relative_to(output.resolve()):
            continue
        obj = json.loads(path.read_text())
        if "tasks" not in obj:
            continue
        manifests[str(path)] = digest(obj)
        for row in obj["tasks"]:
            prior_ids.add(row["id"])
            prior_tasks.add(row["hash"])
            prior_groups.add(row.get("source_group"))
            control = path.parent / "controller" / (row["id"] + ".json")
            if row.get("repository") == REPOSITORY and control.exists():
                # Only controller metadata leaves this block. No historical task,
                # solution, hidden tests or issue text enters the learning bundle.
                meta = json.loads(control.read_text())
                fixes.add(meta.get("fixed_commit"))
                patches.add(meta.get("patch_fingerprint"))
                functions.update(x.removeprefix("src/") for x in meta.get("functions", ()))
    for row in json.loads(external.read_text())["cases"]:
        if row["repository"] == REPOSITORY:
            fixes.add(row["fixed_commit"])
    checks = []
    for task, control in cases:
        touched = {f.removeprefix("src/") for f in control["functions"]}
        if (task.id in prior_ids or digest(task.model_dump()) in prior_tasks or task.source_group in prior_groups
                or control["fixed_commit"] in fixes or control["patch_fingerprint"] in patches or touched & functions):
            raise ValueError("overlap with a frozen task or prior new learning case: " + task.id)
        prior_ids.add(task.id)
        prior_tasks.add(digest(task.model_dump()))
        prior_groups.add(task.source_group)
        fixes.add(control["fixed_commit"])
        patches.add(control["patch_fingerprint"])
        functions.update(touched)
        checks.append({"task_id": task.id, "task_hash": digest(task.model_dump()), "group": task.source_group,
                       "base_commit": control["base_commit"], "fixed_commit": control["fixed_commit"],
                       "functions": sorted(touched), "module_change": control["module_change"],
                       "fingerprint": control["patch_fingerprint"],
                       "description": control["description"], "overlap_found": False})
    return {"schema": 1, "passed": True, "model_calls": 0, "historical_manifest_hashes": manifests,
            "legacy_catalog_hash": digest(json.loads(external.read_text())), "tasks": checks,
            "limits": "Same upstream repository; independence is by task, fix, patch, changed named function "
                      "and defect group. Historical snapshots and dependencies can share source files."}


def prepare(repo, experiments, output, external):
    if output.exists():
        raise ValueError("learning bundle destination must be new")
    cases = [task_case(repo, case) for case in CASES]
    receipt = audit(cases, experiments, output, external)
    manifest = write_bundle(cases, output)
    manifest.update(family="Independent Click CLI rendering procedure learning", seed_task_id=None,
                    public_nodes=[REPRODUCTION + "::test_feature_" + switch for switch in ("enabled", "disabled")])
    (output / "manifest.json").write_text(canonical(manifest) + "\n")
    (output / "independence.json").write_text(canonical({**receipt, "manifest_hash": digest(manifest)}) + "\n")
    return manifest


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--repo", type=Path, default=Path("work/upstream/click.git"))
    p.add_argument("--experiments", type=Path, default=Path("experiments"))
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--legacy-catalog", type=Path, default=Path("benchmarks/realworld/cases.json"))
    args = p.parse_args()
    m = prepare(args.repo, args.experiments, args.output, args.legacy_catalog)
    print(digest(m))
