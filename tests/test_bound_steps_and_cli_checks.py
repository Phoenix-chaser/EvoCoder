import json
import sys
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError

from evocoder.contracts import CheckAction, Skill, digest
from evocoder.model import Model
from evocoder.runtime import Runner
from evocoder.step_contract import StepRevision, bind_revision
from test_budget_model import live_settings
from test_public_checkpoints import example_task
from test_recovery_docker import reply

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from verify_authored_cli_checks import check_source, verdict  # noqa: E402


def test_historical_facts_must_share_run_sequence_and_patch():
    parent = Skill(name="cli-render", parent=None, applicability=["CLI output"], steps=["old factual story"],
                   exclusions=["not a CLI"], evidence_refs=["old"], verification=["old"],
                   counterexamples=["false historical example"], change_summary="Prior version")
    step = {"when": "if_uncovered", "action": "write_cli_check", "target": "missing_states", "observable": "output"}
    rows = [{"observation_seq": 1, "named_states": {"enabled": "passed"}, "patch_hash": "a", "output_hash": "x"},
            {"observation_seq": 3, "named_states": {"enabled": "failed", "disabled": "failed"},
             "patch_hash": "b", "output_hash": "y"}]
    catalog = {"receipts": {"learn": {"procedure": {"solver_requested_public_tests": rows}}}}
    fact = {"run_id": "learn", "observation_seq": 3, "enabled": "passed", "disabled": "failed"}
    proposal = {"steps": [step], "evidence_refs": ["learn"], "facts": [fact]}
    with pytest.raises(ValueError, match="one matching"):
        bind_revision(proposal, parent, catalog)
    with pytest.raises(ValueError, match="one matching"):
        bind_revision({**proposal, "facts": [{**fact, "run_id": "other"}]}, parent, catalog)
    with pytest.raises(ValidationError):
        StepRevision.model_validate({**proposal, "steps": [{**step, "explanation": "historically both passed"}]})
    fact.update(observation_seq=1, disabled="unreported")
    child, [witness] = bind_revision(proposal, parent, catalog)
    assert witness["patch_hash"] == "a" and witness["output_hash"] == "x"
    assert child.parent == parent.hash and "false historical example" not in child.counterexamples
    assert "disabled=unreported" in child.counterexamples[0]
    assert child.applicability == parent.applicability


def test_replay_proof_needs_invocation_observable_change_and_own_assertion():
    base = {"timeout": False, "image": "frozen", "termination": "ok", "exit": 0, "calls": [{"output_hash": "plain"}]}
    off = {**base, "exit": 1, "termination": "authored_assertion", "calls": [{"output_hash": "colored"}]}
    assert verdict(base, base, off, 0, "frozen") == "detects_disabled_regression"
    assert verdict(base, base, {**off, "termination": "error"}, 0, "frozen") == "exercises_cli_only"
    assert verdict(base, base, {**off, "calls": base["calls"]}, 0, "frozen") == "exercises_cli_only"
    assert verdict({**base, "calls": []}, base, off, 0, "frozen") == "unverified"
    assert verdict(base, base, off, 1, "frozen") == "unverified"


def test_only_authored_inline_python_eligible():
    snippet = "from click.testing import CliRunner\nassert 1"
    for action in ({"tool": "cli_check", "content": snippet},
                   {"tool": "command", "argv": ["python", "-c", snippet]},
                   {"tool": "command", "argv": ["bash", "-lc", "python - <<'PY'\n" + snippet + "\nPY"]}):
        assert check_source(action) == snippet
    for argv in (["python", "public_test.py"], ["python", "-m", "pytest"], ["sh", "-c", "cat proof.py"]):
        assert check_source({"tool": "command", "argv": argv}) is None
    with pytest.raises(ValueError, match="standalone"):
        CheckAction(tool="cli_check", content=" ")
    with pytest.raises(ValueError, match="standalone"):
        CheckAction(tool="cli_check", content="assert True" + " " * 12000)


@pytest.mark.docker
def test_authored_script_is_executed_on_identified_clean_action_patch(settings, store):
    task = example_task()
    opts = live_settings(settings).model_copy(update={"authored_cli_checks": True, "public_checkpoints": True})
    body = "from pathlib import Path\nfrom value import value\nassert not Path('private.py').exists()\nassert value == 1\nprint('checked')"
    actions = iter([{"tool": "edit", "path": "value.py", "old_text": "0", "new_text": "1"},
                    {"tool": "cli_check", "content": body}, {"tool": "finish"}])

    def provider(request):
        msg = json.loads(request.content)
        assert "cli_check" in msg["tools"][0]["function"]["parameters"]["properties"]["tool"]["enum"]
        return httpx.Response(200, json=reply(next(actions)))

    k = Runner(opts, store, Model(opts, store, httpx.MockTransport(provider))).solve(task, use_active=False)
    assert store.run(k)["state"] == "COMPLETE"
    [event] = [e["payload"] for e in store.events(k) if e["kind"] == "AUTHORED_CLI_CHECK"]
    assert event["task_hash"] == digest(task.model_dump()) and event["exit_code"] == 0
    assert event["output_hash"] == digest("checked\n")
    assert store.get_artifact(event["script"]) == {"source": body}
    assert store.get_artifact(event["patch"])["value.py"] == "value = 1\n"


def test_portable_example_rejects_fabricated_states_and_changed_source():
    from replay_click_example import apply, edits
    from verify_step_example import verify
    data = json.loads((Path(__file__).resolve().parents[1] / "examples/click-step-seven.json").read_text())
    assert verify(data)["observation"] == 22
    data["admission"]["positive_control"]["enabled"] = "passed"
    with pytest.raises(ValueError, match="witness"):
        verify(data)
    original = {"source.py": "a\nb\nc\nd\n"}
    edited = {"source.py": "head\na\nc\nend\n"}
    hunks = edits(original, edited)
    assert apply(original, hunks) == edited
    with pytest.raises(ValueError, match="archived edit"):
        apply({"source.py": "other\ncode\n"}, hunks)


def test_off_check_must_reach_same_input_on_the_model_action_patch():
    from verify_cli_reach import indices
    on = {"input": {"stack": [8], "color": True}, "exit": 0, "output_hash": "styled"}
    off = {"input": {"stack": [11], "color": False}, "exit": 0, "output_hash": "plain"}
    bad = {**off, "output_hash": "styled"}
    assert indices([on], [on, off], [on, bad]) == []
    assert indices([on, on], [on, off], [on, bad]) == []
    assert indices([on, bad], [on, off], [on, bad]) == [1]


def test_directed_retry_reuses_saved_model_reply_and_requires_real_assert_node(store):
    from types import SimpleNamespace
    from repair_click_authored_checks import has_assertion, request_check
    requests = []

    def request(schema, prompt, data, rid):
        requests.append(rid)
        return CheckAction(tool="cli_check", content="assert isinstance(2, int)")

    model = SimpleNamespace(transport=SimpleNamespace(last=None), request=request)
    first = request_check(model, store, "once", {})
    second = request_check(model, store, "once", {})
    assert first == second and requests == ["once"]
    assert has_assertion(first["source"])
    assert not has_assertion("print('assert False')")
    assert not has_assertion("assert =")


def test_duplicate_green_coverage_uses_observed_completion_inputs():
    from check_cli_coverage_feedback import choice_modes, public_choice
    assert public_choice("x = click.Choice(metals, case_sensitive=True)") is True
    with pytest.raises(ValueError, match="exactly one"):
        public_choice("x = click.Choice(metals, case_sensitive=flag)")
    calls = [
        {"input": {"kwargs": {"env": {"_TOOL_COMPLETE": "bash_complete"}}, "options": [["a", None, True]]}},
        {"input": {"kwargs": {"env": {}}, "options": [["a", None, False]]}},
    ]
    assert choice_modes(calls) == [True]
    calls.append({"input": {"kwargs": {"env": {"_TOOL_COMPLETE": "bash_complete"}}, "options": [["a", None, False]]}})
    assert choice_modes(calls) == [False, True]
