from types import SimpleNamespace

import pytest

from evocoder import route_study as study


def test_schedule_balances_order_within_every_task_and_policy():
    schedule = study.schedule()
    assert len(schedule) == 12
    for task in study.CASES:
        for policy in study.POLICIES:
            pair = [c for c in schedule if c["task_id"] == task and c["policy"] == policy]
            assert [c["repeat"] for c in pair] == [0, 1]
            assert {c["reverse"] for c in pair} == {True, False}


@pytest.mark.parametrize("remaining_wins, support_regression, unlock", [
    (False, False, False), (True, True, False), (True, False, True),
])
def test_only_full_normal_gate_can_open_holdouts(tmp_path, monkeypatch, remaining_wins, support_regression, unlock):
    a, b = study.CASES
    tasks = {k: SimpleNamespace(id=k, model_dump=lambda: {"v": 1}) for k in [a,b,"third","old","support","new"]}
    protocol = {"tasks": {a:a,b:b}, "prior_dev_tasks": [a,b,"third"], "regression_tasks": ["old"],
                "support_tasks": ["support"], "test_tasks": ["new"], "repeats": 2, "holdout_repeats": 2,
                "origin": str(tmp_path/"origin")}
    completed, claims = [], []

    def pair(lib, task, repeat, policy, reverse, config):
        k = task.id
        completed.append((k, policy))
        return {"task_id": k, "source_group": k, "split": "learn" if k == "support" else "dev",
                "repeat": repeat, "policy": policy, "baseline_pass": k == "support" and support_regression,
                "candidate_pass": k == a or (k == "third" and remaining_wins), "infrastructure_error": False,
                "skill_behavior": "candidate_applied"}

    result = {"rows": [pair(None,tasks[k],rep,"normal",False,protocol) for rep in range(2) for k in (a,b)],
              "full_gate": {}, "holdout": {"rows": []}}
    lib = SimpleNamespace(store=SimpleNamespace(ledger=None, claim_test=lambda k,h,e: claims.append(k)))
    monkeypatch.setattr(study, "pair", pair)
    monkeypatch.setattr(study, "check_fresh_holdouts", lambda *args: len(claims))
    study.remaining_gates(lib, tasks, protocol, result, tmp_path)
    assert result["full_gate"]["gate"]["passed"] is unlock
    assert claims == (["new","new"] if unlock else [])
    assert all(policy == "normal" for _,policy in completed)
    assert sum(k in {a,b} for k,_ in completed) == 4
