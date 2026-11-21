from evocoder.evolution import Evolution


def events(failure_before_edit=False):
    result = [{"seq": 1, "kind": "START", "payload": {"task": {"public_command": ["python", "tests.py"]}}}]
    tools = [("test", 1), ("write", 0), ("test", 0)] if failure_before_edit else [
        ("write", 0), ("test", 1), ("write", 0), ("test", 0), ("write", 0)]
    for i, (tool, status) in enumerate(tools, 2):
        result.append({"seq": i, "kind": "OBSERVATION", "payload": {
            "action": {"tool": tool}, "observation": {"exit_code": status}}})
    result.append({"seq": 99, "kind": "GRADE", "payload": {"hidden": "must never be feedback"}})
    return result


def test_recovered_post_edit_failure_is_distinct_from_final_failure(settings, store):
    feedback = Evolution.execution_feedback({"state": "COMPLETE"}, events())
    episode = {"public_failed": False, "execution_feedback": feedback}
    assert feedback["post_edit_public_failure_sequences"] == [3]
    assert feedback["writes_after_last_public_success"] == 1
    assert not Evolution(settings, store).qualifies(episode)
    assert Evolution(settings, store, feedback_policy="post_edit_or_budget").qualifies(episode)


def test_original_bug_failure_before_agent_edit_does_not_qualify(settings, store):
    feedback = Evolution.execution_feedback({"state": "COMPLETE"}, events(True))
    assert not feedback["post_edit_public_failure_sequences"]
    assert not Evolution(settings, store, feedback_policy="post_edit_or_budget").qualifies({
        "public_failed": False, "execution_feedback": feedback})


def test_step_exhaustion_is_explicit_procedural_feedback(settings, store):
    feedback = Evolution.execution_feedback({"state": "EXHAUSTED"}, events(True))
    assert feedback["step_budget_exhausted"]
    assert Evolution(settings, store, feedback_policy="post_edit_or_budget").qualifies({
        "public_failed": False, "execution_feedback": feedback})
    assert "hidden" not in str(feedback)


def test_unrepaired_original_failure_without_edit_is_not_skill_failure(settings, store):
    feedback = Evolution.execution_feedback({"state": "FAILED"}, events(True)[:2])
    episode = {"public_failed": True, "execution_feedback": feedback}
    assert Evolution(settings, store).qualifies(episode)
    assert not Evolution(settings, store, feedback_policy="post_edit_or_budget").qualifies(episode)


def test_exact_edit_counts_as_agent_change_for_execution_feedback():
    trace = events()
    trace[1]["payload"]["action"]["tool"] = "edit"
    assert Evolution.execution_feedback({"state": "FAILED"}, trace)["post_edit_public_failure_sequences"] == [3]
