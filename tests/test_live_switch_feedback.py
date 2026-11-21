import json
import sys
from pathlib import Path
from types import SimpleNamespace as NS

import httpx
import pytest

from evocoder.click_observer import modes_for
from evocoder.contracts import Task, digest
from evocoder.model import Model
from evocoder.runtime import Runner
from evocoder.switch_feedback import SwitchFeedback, axis_from_public
from test_budget_model import live_settings
from test_recovery_docker import reply


def test_public_example_and_real_invocations_have_separate_meanings():
    example = "public_checks/test_reported_layout.py"
    files = {example: 'x = click.Choice(["Oak"], case_sensitive=False)',
             "test_other.py": "click.option('-v', count=True)"}
    assert axis_from_public(files, list(files), "issue") == "case_insensitive"
    assert axis_from_public(files, ["python", "missing.py"], "case_sensitive=False") is None
    feedback = SwitchFeedback("case_insensitive", "patch1")
    assert feedback.view()["not_yet_observed"] == ["disabled", "enabled"]
    feedback.record("patch1", "public", {"axis": "count", "modes": [False]})
    feedback.record("patch1", "public", {"axis": "case_insensitive", "modes": [True]})
    assert feedback.view()["not_yet_observed"] == ["disabled"]
    feedback.patch = "patch2"
    assert feedback.view()["observed_in_public_test"] == []


def test_help_and_environment_are_measured_on_the_relevant_path():
    arg = NS(opts=["--gem"], secondary_opts=[], name="gem", count=False, type=NS(min=1, max=4),
             envvar=None, allow_from_autoenv=False)
    cli = NS(context_settings={"auto_envvar_prefix": "BOX"}, params=[arg], add_help_option=False)
    out = NS(exit_code=0, output="Options:\n  --gem  number")
    assert modes_for("count", cli, [], {}, {}, out) == set()
    assert modes_for("count", cli, ["--help"], {}, {}, out) == {False}
    assert modes_for("add_help_option", cli, [], {}, {}, out) == set()
    assert modes_for("allow_from_autoenv", cli, [], {}, {}, out) == set()
    assert modes_for("allow_from_autoenv", cli, [], {"env": {"BOX_GEM": "opal"}}, {}, out) == {False}
    assert modes_for("allow_from_autoenv", cli, ["--gem=3"], {"env": {"BOX_GEM": "opal"}}, {}, out) == set()


def test_previous_bash_completion_observation_requires_actual_selected_choice():
    files = {"public.py": "x = click.Choice(['Y'], case_sensitive=True)\ne = {'_TOOL_COMPLETE':'bash_complete'}"}
    assert axis_from_public(files, ["python", "public.py"], "") == "completion_case_sensitive"
    p = NS(type=NS(case_sensitive=False), opts=["--gem"])
    cli = NS(name="tool", params=[p], context_settings={})
    env = {"_TOOL_COMPLETE": "bash_complete", "COMP_CWORD": "2", "COMP_WORDS": "tool --gem op"}
    out = NS(exit_code=0, output="plain,opal\n")
    assert modes_for("completion_case_sensitive", cli, [], {"env": env}, {}, out) == {False}
    env["COMP_WORDS"] = "tool --name op"
    assert modes_for("completion_case_sensitive", cli, [], {"env": env}, {}, out) == set()


def test_weak_or_unbound_off_checks_do_not_receive_credit():
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    from verify_switch_trials import strict_result

    call = {"input": {"stack": [1], "modes": [False]}, "exit": 0, "output_hash": "x"}
    orig = {"calls": [call], "termination": "ok", "exit": 0}
    mutant = {"calls": [{**call, "output_hash": "y"}], "termination": "authored_assertion", "exit": 1, "line": 1}
    for script, raw, original, caught in [
        ("print('checked')", True, orig, False), ("assert x", False, orig, False),
        ("assert x", True, {**orig, "calls": []}, False), ("assert x", True, orig, True),
    ]:
        assert strict_result(original, orig, mutant, script, raw, 0)["detected"] is caught


@pytest.mark.docker
def test_runner_delivers_measured_modes_on_next_request_and_restores_by_exact_patch(settings, store):
    app = (
        "from types import SimpleNamespace as N\nfrom click.testing import CliRunner\n"
        "def check(t):\n p=N(param_type_name='option',type=t,opts=['--x'],secondary_opts=[])\n"
        " r=CliRunner().invoke(N(params=[p]), ['--x','a'])\n assert r.exit_code==0\n"
    )
    public = "from app import check\nfrom click import Choice\ncheck(Choice(['a'], case_sensitive=False))\n"
    task = Task(id="switch-fixture", repository="fixture/cli", split="learn", source_group="fixture/choice",
                issue="Choice matching", files={
                    "app.py": app, "marker.py": "version = 1\n", "public.py": public,
                    "click/__init__.py": "class Choice:\n def __init__(self, values, case_sensitive): self.case_sensitive=case_sensitive\n",
                    "click/testing.py": "from types import SimpleNamespace as N\nclass CliRunner:\n env={}\n def invoke(self, cli, args): return N(exit_code=0,output='a')\n",
                }, editable=["marker.py"], public_command=["env", "PYTHONPATH=/workspace", "python", "public.py"],
                grader_files={"sealed.py": "assert True\n"}, grader_command=["python", "sealed.py"])
    original = digest({"marker.py": task.files["marker.py"]})
    actions = iter([{"tool": "test"}, {"tool": "cli_check", "content": public.replace("False", "True")},
                    {"tool": "edit", "path": "marker.py", "old_text": "1", "new_text": "2"}, {"tool": "test"},
                    {"tool": "restore_checkpoint", "checkpoint_id": original}, {"tool": "finish"}])
    views = []

    def provider(req):
        content = json.loads(req.content)["messages"][1]["content"]
        assert "sealed.py" not in content
        data = json.loads(content)
        views.append((data["remaining_steps"], data["public_switch_coverage"]))
        return httpx.Response(200, json=reply(next(actions)))

    opts = live_settings(settings).model_copy(update={"public_checkpoints": True, "live_switch_feedback": True, "max_steps": 6})
    key = Runner(opts, store, Model(opts, store, httpx.MockTransport(provider))).solve(task, use_active=False)
    run = store.run(key)
    assert run["state"] == "COMPLETE" and run["result"]["model_steps"] == 6
    assert [v for v, _ in views] == [6, 5, 4, 3, 2, 1]
    assert [v["observed_in_public_test"] for _, v in views] == [[], ["enabled"], ["enabled"], [], ["enabled"], ["enabled"]]
    assert [v["observed_in_own_checks"] for _, v in views] == [[], [], ["disabled"], [], [], ["disabled"]]
