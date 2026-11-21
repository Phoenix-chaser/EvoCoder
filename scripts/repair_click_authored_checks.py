"""Bounded GLM repair of its own failing assertions on already exposed learning snapshots."""
import argparse
import ast
import fcntl
import time
from pathlib import Path

from continue_route_study import ObserveRateLimit
from evocoder.comparison import dump
from evocoder.contracts import Action, Settings, digest, model_task_view
from evocoder.errors import ActionRejected
from evocoder.model import Model
from evocoder.revision_diagnostic import read
from evocoder.runtime import validation
from evocoder.store import Store
from report_click_procedure import Archive
from run_click_procedure import options_for
from verify_authored_cli_checks import ENV, entries, mutant, replay, verdict
from verify_cli_reach import indices, observed

PROMPT = """Repair a check you authored for a CLI on a frozen learning workspace. Use the public issue, your earlier
script and its actual output. Choose a required behavior absent from the supplied public example. Write a small,
standalone CLI check whose own assertion compares the observed output or status with the public contract.
Keep unrelated assertions from preventing that case from executing. You can split off a single example,
or inspect the actual output before trying again. Only submit_action with tool cli_check and Python source
in content is executable. Product edits are unavailable. You have up to three check actions.
The system provides no expected strings, example implementation or private grader results."""


def subjects(root):
    result = read(root / "result.json")
    store = Archive(root / "seed/evocoder.sqlite3")
    rows = []
    for role, trials in (("parent", result["learning"]), ("child", result["learning_replay"])):
        for trial in trials:
            key = trial["run_id"]
            events = store.events(key)
            selected = []
            for before, event, after in zip(events, events[1:], events[2:]):
                if event["kind"] != "AUTHORED_CLI_CHECK" or before["kind"] != "ACTION" or after["kind"] != "OBSERVATION":
                    continue
                action, obs, receipt = before["payload"], after["payload"]["observation"], event["payload"]
                if (action["tool"] == "cli_check" and receipt["exit_code"] == obs["exit_code"] == 1
                        and not receipt["timeout"] and "AssertionError" in obs["output"]
                        and receipt["output_hash"] == digest(obs["output"])
                        and store.get_artifact(receipt["script"]) == {"source": action["content"]}):
                    selected.append({"role": role, "task": trial["task_id"], "run": key, "seq": event["seq"],
                                     "source": action["content"], "output": obs["output"], "patch": receipt["patch"],
                                     "task_hash": receipt["task_hash"], "script_artifact": receipt["script"]})
            if selected:
                rows.append(selected[-1])
    return rows


def register(root):
    result = read(root / "result.json")
    p = result["protocol"]
    dest = root / "check-repair"
    if dest.exists() or not result["completed"] or p["claim"] != "missing_cli_state_revision":
        raise ValueError("registration requires a closed study and a fresh diagnostic directory")
    selected = subjects(root)
    if not selected or any(s["task"] not in p["learn_ids"] for s in selected):
        raise ValueError("no eligible actual failed learning assertion")
    plan = {"parent_study": digest(result), "driver": digest(Path(__file__).read_text()),
            "prompt": PROMPT, "max_attempts": 3, "registered_at": time.time(), "subjects": selected,
            "selection": "Last live solver-authored CLI action exiting 1 with AssertionError on each parent/child "
                         "learning trial. No score, dev trace or off-mutant outcome used for selection or prompt.",
            "stop": "First successful action-time script containing a Python assert, or three attempts.",
            "scope": "Separate directed check-repair budget on the exact same archived source. No product edits, "
                     "new cases, candidate registration, independent grading feedback or promotion."}
    dump(dest / "plan.json", plan)
    (dest / Path(__file__).name).write_text(Path(__file__).read_text())
    return plan


def has_assertion(source):
    try:
        return any(isinstance(node, ast.Assert) for node in ast.walk(ast.parse(source)))
    except SyntaxError:
        return False


def request_check(model, st, rid, payload):
    events = st.events(rid)
    saved = [e["payload"] for e in events if e["kind"] == "CHECK_REPAIR_REPLY"]
    if saved:
        return saved[0]
    if any(e["kind"] == "MODEL_RESPONSE" for e in events):
        raise ValueError("model response exists without a saved action; inspect before resuming")
    for retry in range(3):
        model.transport.last = None
        try:
            action = model.request(Action, PROMPT, payload, rid)
            reply = {"tool": action.tool, "source": action.content if action.tool == "cli_check" else ""}
        except ActionRejected:
            reply = {"tool": "invalid_action", "source": ""}
        except RuntimeError as exc:
            rate = model.transport.last or {}
            if "model_http_429" not in str(exc) or rate.get("code") not in {1302, 1305} or retry == 2:
                raise
            st.event(rid, "CHECK_REPAIR_BACKOFF", rate)
            time.sleep(max((60, 180)[retry], rate.get("retry_after_seconds") or 0))
            continue
        st.event(rid, "CHECK_REPAIR_REPLY", reply)
        return reply
    raise ValueError("unreachable retry state")


def execute(root, settings):
    dest = root / "check-repair"
    plan, main = read(dest / "plan.json"), read(root / "result.json")
    p = main["protocol"]
    if (plan["driver"] != digest(Path(__file__).read_text()) or plan["parent_study"] != digest(main)
            or plan["subjects"] != subjects(root) or plan["prompt"] != PROMPT):
        raise ValueError("changed targeted continuation")
    if (dest / "result.json").exists():
        raise ValueError("this diagnostic is complete")
    opts = options_for(settings, p["settings"], True)
    ledger = Store(opts.state_dir, unit=opts.accounting_unit)
    with (ledger.root / "comparison.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        st = Store(dest / "state", ledger=ledger)
        model = Model(opts, st, ObserveRateLimit())
        prior = Archive(root / "seed/evocoder.sqlite3")
        data = entries(Path(p["learn_manifest"]), list(dict.fromkeys(s["task"] for s in plan["subjects"])))
        start = dest / "start.json"
        if not start.exists():
            dump(start, {"plan": digest(plan), "budget": ledger.budget()})
        if read(start)["plan"] != digest(plan):
            raise ValueError("mismatched accounting window")
        results = []
        for index, subject in enumerate(plan["subjects"]):
            task, _ = data[subject["task"]]
            if task.split != "learn" or digest(task.model_dump()) != subject["task_hash"]:
                raise ValueError("changed learning task")
            patch = prior.get_artifact(subject["patch"])
            if set(patch) != set(task.editable):
                raise ValueError("incomplete archived patch")
            history = []
            for step in range(plan["max_attempts"]):
                rid = digest([digest(plan), index, step])[:32]
                # No grader data, upstream fix, mutation, other agent, or gate result crosses this boundary.
                payload = {"task": model_task_view(task.public()), "public_example": task.files["public_checks/test_reported_layout.py"],
                           "earlier_source": subject["source"], "earlier_output": subject["output"],
                           "check_attempts": history, "remaining_actions": plan["max_attempts"] - step}
                reply = request_check(model, st, rid, payload)
                body = reply["source"]
                obs = (validation({**task.files, **patch}, [*ENV, body], opts, "repair-check-" + rid[:12])
                       if body else {"exit_code": 2, "timeout": False, "output": "No CLI script submitted."})
                item = {"id": rid, "attempt": step + 1, "source": body, "output": obs["output"][:10000],
                        "exit": obs["exit_code"], "timeout": obs["timeout"], "source_hash": digest(body)}
                history.append(item)
                if body and not obs["timeout"] and obs["exit_code"] == 0 and has_assertion(body):
                    break
            results.append({"subject": index, "role": subject["role"], "task": task.id, "run": subject["run"],
                            "seq": subject["seq"], "patch": subject["patch"], "attempts": history})
            dump(dest / "live-actions.json", results)
        # Grade only after every model response. Replays never feed back into these attempts.
        for trial in results:
            task, ref = data[trial["task"]]
            patch = prior.get_artifact(trial["patch"])
            off = mutant(task, ref)
            for action in trial["attempts"]:
                body = action["source"]
                if not body:
                    action["caught_disabled"] = False
                    continue
                a, b, c = [replay(task, v, body, opts) for v in (patch, ref, off)]
                base = verdict(a, b, c, action["exit"], opts.image)
                live = validation({**task.files, **patch}, [*ENV, body], opts, "repair-check-replay")
                exact = (live["exit_code"] == action["exit"] and not live["timeout"] and not action["timeout"]
                         and live["output"][:10000] == action["output"])
                ia, ib, ic = [observed(task, v, body, opts) for v in (patch, ref, off)]
                hit = indices(ia, ib, ic)
                action.update(original_action=a, reference=b, off_mutant=c, original_calls=ia, reference_calls=ib,
                              mutant_calls=ic, matched_call_indices=hit, raw_execution_matches=exact,
                              caught_disabled=bool(exact and hit and base == "detects_disabled_regression"))
        ids = [a["id"] for t in results for a in t["attempts"]]
        before, after, costs = read(start)["budget"], ledger.budget(), st.costs(ids)
        if abs(after["spent_credits"] - before["spent_credits"] - costs["cost_credits"]) > 1e-6:
            raise ValueError("check repair accounting mismatch")
        report = {"plan": digest(plan), "main": digest(main), "model": opts.model, "image": opts.image,
                  "trials": results, "costs": costs, "before": before, "after": after,
                  "holdout_tasks": [], "registered_skills": 0, "used_for_promotion": False}
        dump(dest / "result.json", report)
        return report


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("command", choices=["register","run"])
    ap.add_argument("--root", type=Path, required=True)
    ap.add_argument("--env-file", type=Path, default=Path(".env.glm"))
    args = ap.parse_args()
    if args.command == "register":
        plan = register(args.root.resolve())
        print([(s["role"],s["task"],s["seq"]) for s in plan["subjects"]])
    else:
        r = execute(args.root.resolve(), Settings.from_env_file(args.env_file))
        print([(t["role"],any(a["caught_disabled"] for a in t["attempts"])) for t in r["trials"]], r["costs"])
