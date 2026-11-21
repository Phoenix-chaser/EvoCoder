"""Verify portable results, rebuild the dataset offline, or export a finished study without model calls."""
import argparse
import json
import subprocess
from collections import Counter
from pathlib import Path
from tempfile import TemporaryDirectory

from evocoder.comparison import dump
from evocoder.contracts import Settings, digest
from evocoder.evolution import Evolution
from evocoder.gate import gate_summary, skill_behavior
from evocoder.jobs import bounded_map
from evocoder.optional_benchmark import VALUES, calibrate, catalog, checks, expected, freeze_dataset
from evocoder.runtime import validation
from measure_skill_uptake import action_distance, summarize
from report_click_procedure import Archive, read
from run_optional_control import curve_point, first_round_gate

PUBLIC = Path("docs/results")
REGISTRATION = Path("benchmarks/optional_control/protocol.json")


def check(condition, message):
    if not condition:
        raise ValueError(message)


def verify_uptake(data):
    grouped = {}
    for phase in ("matrix", "switch"):
        for pair in data[phase]["pairs"]:
            arms = [pair[k] for k in ("baseline", "candidate")]
            check(all(a["steps"] == sum(a["counts"].values()) and
                      all(type(n) is int and n > 0 for n in a["counts"].values()) for a in arms), "bad action counts")
            tv = action_distance(*(a["counts"] for a in arms))
            check(float(tv) == pair["tv"], "bad within-pair distance")
            key = (phase, pair["policy"], pair["task_id"], pair["repeat"])
            check(key not in grouped, "duplicate task/repeat")
            grouped[key] = tv
    deltas = [v - grouped[phase, "fixed_skip", task, rep] for (phase, policy, task, rep), v in grouped.items()
              if policy == "fixed_apply"]
    check(deltas and (sum(x > 0 for x in deltas), len(deltas)) ==
          (data["uptake_numerator"], data["uptake_denominator"]), "wrong uptake rate")
    check(abs(float(sum(deltas) / len(deltas)) - data["mean_excess_tv"]) < 1e-12, "wrong excess TV")
    for policy in data["matrix_by_policy"]:
        check(summarize([p for p in data["matrix"]["pairs"] if p["policy"] == policy]) ==
              data["matrix_by_policy"][policy], "wrong policy totals")
    return len(grouped)


def boundary_tests(scenario, index):
    field = scenario.fields[index]
    default, normal, empty, _ = VALUES[index]
    missing, falsy = [{}, {field: None}], [{field: empty}]
    if scenario.shape == "layers":
        missing = [{"request": v, "tenant": {field: normal}} for v in missing]
        falsy = [{"request": {field: empty}, "tenant": {field: normal}}, {"request": {}, "tenant": {field: empty}}]
    elif scenario.shape == "batch":
        missing = [{"rows": missing}]
        falsy = [{"rows": [{field: normal}, {field: empty}]}]
    return {name: checks([(v, expected(scenario, field, default, v)) for v in inputs])
            for name, inputs in (("missing.py", missing), ("falsy.py", falsy))}


def score_patch(task, patch, probes, options):
    program = ("import json, subprocess, sys\n"
               "print(json.dumps([subprocess.run([sys.executable, '-B', f], stdout=subprocess.DEVNULL, "
               "stderr=subprocess.DEVNULL).returncode for f in ('missing.py', 'falsy.py')]))\n")
    result = validation({**task.files, **patch, **probes, "audit.py": program},
                        ["python", "audit.py"], options, "optional-probe-" + task.id)
    check(result["exit_code"] == 0 and result["image_id"] == options.image, "boundary-probe infrastructure error")
    exits = json.loads(result["output"])
    check(len(exits) == 2 and all(code in (0, 1) for code in exits), "unexpected boundary-probe result")
    return dict(zip(("missing", "valid_falsy"), [code == 0 for code in exits], strict=True))


def export(root, registered):
    protocol, result = read(root / "protocol.json"), read(root / "result.json")
    check(protocol == registered and result["completed"] and result["protocol"] == digest(protocol), "unbound run")
    check(not result["unassigned_billing_run_count"], "incomplete billing attribution")
    cal = read(root / "calibration.json")
    check(digest(cal) == protocol["calibration"], "calibration does not match registration")
    manifest = read(root / "dataset/manifest.json")
    check(digest(manifest) == protocol["manifest"], "changed task catalog")
    tasks = {task.id: (scenario, task) for scenario, task, _, _ in catalog()}
    archive = Archive(root / "state/evocoder.sqlite3")
    options = Settings(_env_file=None, mode="mock", image=protocol["image"])
    cached = {}

    def attempt(st, key, task_id, *, expected_split):
        run = st.run(key)
        scenario, task = tasks[task_id]
        check((run["task_id"], run["task_hash"], run["split"], run["mode"]) ==
              (task_id, digest(task.model_dump()), expected_split, "live"), "wrong live run")
        check(run["state"] in ("COMPLETE", "FAILED", "EXHAUSTED"), "incomplete solver")
        events = st.events(key)
        final = run["result"]
        route, = [e["payload"] for e in events if e["kind"] == "SKILL_ROUTE"]
        check(final["image_id"] == protocol["image"] and route["applied_skill_id"] == run["skill_id"], "image/skill mismatch")
        actions = Counter(e["payload"]["tool"] for e in events if e["kind"] == "ACTION")
        check(sum(actions.values()) <= protocol["solver_slots_per_arm"], "unequal action allowance")
        evidence = None
        if final["patch"]:
            patch = st.get_artifact(final["patch"])
            cache_key = (task_id, final["patch"])
            if cache_key not in cached:
                index = int(task_id.rsplit("-", 1)[1]) - 1
                cached[cache_key] = score_patch(task, patch, boundary_tests(scenario, index), options)
            evidence = cached[cache_key]
        return {"task": task_id, "run": key, "applied": run["skill_id"], "route": route["decision"],
                "passed": final["passed"], "state": run["state"], "patch": final["patch"], "probes": evidence,
                "actions": dict(sorted(actions.items())), "model_calls": final["cost"]["calls"]}

    def pairs(rows):
        out = []
        for r in rows:
            item = {k: r[k] for k in ("task_id", "source_group", "split", "repeat", "skill_behavior", "infrastructure_error")}
            for arm in ("baseline", "candidate"):
                a = attempt(archive, r[arm + "_run"], r["task_id"], expected_split=r["split"])
                check((a["passed"], a["applied"]) == (r[arm + "_pass"], r[arm + "_applied_skill"]), "mismatched score")
                item[arm] = a
            out.append(item)
        return out

    public = {"schema": 1, "protocol": digest(protocol), "model": protocol["model"], "outcome": result["phase"],
              "published": result["published"], "calibration": [{"id": r["id"], "hash": r["hash"], "exit_codes": r["exit_codes"]}
                                                             for r in cal["rows"]],
              "learning": [], "skills": [], "qualifying_failures": result.get("qualifying_failures", []),
              "screen": pairs(result.get("screen", [])), "parent_child": pairs(result.get("parent_child", [])),
              "final_gate": None, "holdout": pairs(result["holdout"]), "curve": result["curve"], "oracle": [],
              "costs": result["costs"]}
    with archive.connect() as db:
        test_ids = {r[0] for r in db.execute("SELECT id FROM runs WHERE split='test'")}
        ledger_path = Path(db.execute("SELECT root FROM ledger_binding WHERE singleton=1").fetchone()[0])
    check(test_ids == {r[a]["run"] for r in public["holdout"] for a in ("baseline", "candidate")},
          "unreported holdout attempt")
    with Archive(ledger_path / "evocoder.sqlite3").connect() as db:
        claims = {r[0] for r in db.execute("SELECT task_id FROM test_exposures WHERE experiment=?", (public["protocol"],))}
    check(claims == {r["task_id"] for r in public["holdout"]}, "different shared-ledger test exposure")
    public["holdout_task_exposures"] = len(claims)
    # Replay the original feedback rule without constructing a billable model.
    engine = Evolution(options, archive, model=object(), feedback_policy="learn_outcome")
    qualifying = []
    for key in [*result["unassisted_learning"], *result.get("application_runs", [])]:
        public["learning"].append(attempt(archive, key, archive.run(key)["task_id"], expected_split="learn"))
        if key in result.get("application_runs", []) and public["learning"][-1]["applied"] == result["parent"]:
            feedback = engine.revision_feedback(archive.run(key), archive.events(key))
            if engine.qualifies({"execution_feedback": feedback}):
                qualifying.append(key)
    check(qualifying == public["qualifying_failures"], "unbound claim about the revision trigger")
    for stage, k in (("formation", result["parent"]), ("revision", result.get("child"))):
        if not k:
            continue
        body = archive.get_artifact(k)
        with archive.connect() as db:
            ev_hash = db.execute("select evidence_hash from skills where id=?", (k,)).fetchone()[0]
            proposals = [r[0] for r in db.execute("select run_id from events where kind='PROPOSED' and json_extract(payload,'$.skill_id')=?", (k,))]
        check(len(proposals) == 1, "ambiguous proposal")
        events = archive.events(proposals[0])
        start, = [e["payload"] for e in events if e["kind"] == "PROPOSAL_START"]
        output, = [e["payload"] for e in events if e["kind"] == "PROPOSAL_OUTPUT"]
        check(start["evidence_hash"] == ev_hash and start["proposal_policy"] == "standard"
              and archive.get_artifact(output["artifact"]) == body, "skill differs from original model output")
        calls = [e["payload"] for e in events if e["kind"] == "MODEL_RESPONSE"]
        check(len(calls) == 1 and calls[0]["expected_function"] == "submit_skill" and
              calls[0]["function_names"] == ["submit_skill"] and calls[0]["provider_model"].lower() ==
              protocol["model"].lower(), "no live model proposal receipt")
        frozen = archive.get_artifact(ev_hash)
        evidence_runs = [ep["run_id"] for ep in frozen["episodes"]]
        check(set(evidence_runs) <= {a["run"] for a in public["learning"]}, "non-learning evidence")
        public["skills"].append({"stage": stage, "id": k, "content": body, "evidence_hash": ev_hash,
                                 "evidence_runs": evidence_runs,
                                 "proposal_request_hash": calls[0]["request_body_hash"]})
    if "gate" in result:
        gate = result["gate"]
        check(archive.get_artifact(gate["receipt"]) == {k: v for k, v in gate.items() if k != "receipt"}, "bad gate")
        public["final_gate"] = {"receipt": gate["receipt"], "passed": gate["gate"]["passed"],
                                "rows": pairs(gate["rows"])}
    if "oracle_anchor_pass" in result:
        oracle = Archive(root / "oracle-state/evocoder.sqlite3")
        with oracle.connect() as db:
            rows = list(db.execute("select id,task_id from runs order by created"))
        check([r[1] for r in rows] == protocol["anchors"], "unbound oracle tasks")
        public["oracle"] = [attempt(oracle, k, t, expected_split="dev") for k, t in rows]
        check([r["passed"] for r in public["oracle"]] == result["oracle_anchor_pass"], "bad oracle scores")
        if "anchor_unassisted" in result:
            with archive.connect() as db:
                runs = dict(db.execute("select task_id,id from runs where split='dev'"))
            public["unassisted_oracle_anchors"] = [attempt(archive, runs[t], t, expected_split="dev") for t in protocol["anchors"]]
    return public


def verify_public(data, protocol):
    check(data["protocol"] == digest(protocol) and data["model"] == protocol["model"], "wrong registration")
    with TemporaryDirectory() as tmp:
        manifest = freeze_dataset(Path(tmp) / "tasks")
    check(digest(manifest) == protocol["manifest"], "generator changed the frozen tasks")
    cal = {"manifest": digest(manifest), "rows": [dict(r, image=protocol["image"]) for r in data["calibration"]]}
    check(digest(cal) == protocol["calibration"], "unbound calibration rows")
    rows = {r["id"]: r for r in manifest["tasks"]}
    for r in cal["rows"]:
        check(r["hash"] == rows[r["id"]]["hash"] and
              r["exit_codes"] == [[1, 1], [0, 0], [int(rows[r["id"]]["split"] == "learn"), 1]], "invalid case")
    learn = {a["run"]: a for a in data["learning"]}
    skills = data["skills"]
    root = skills[0]["id"] if skills else None
    candidate = skills[-1]["id"] if skills else None
    expected_learning = protocol["learning_seed"] + (protocol["learning_trials"] if skills else [])
    check([a["task"] for a in data["learning"]] == expected_learning and len(learn) == len(expected_learning),
          "incomplete or duplicated learning run")
    for a in data["learning"]:
        allowed = (None,) if a["task"] in protocol["learning_seed"] else (None, root)
        check(a["applied"] in allowed and rows[a["task"]]["split"] == "learn", "wrong learning exposure")
    seed = {k for k, a in learn.items() if a["task"] in protocol["learning_seed"]}
    failures = data["qualifying_failures"]
    check(len(failures) == len(set(failures)) and all(k in learn and k not in seed and learn[k]["applied"] == root
                                                      for k in failures), "invalid revision evidence")
    check(len(skills) <= 1 + bool(failures), "revision without a qualifying application")
    for index, skill in enumerate(skills):
        content = skill["content"]
        allowed = seed if index == 0 else seed | set(failures)
        refs = set(content["evidence_refs"])
        check(digest(content) == skill["id"] and refs and refs <= set(skill["evidence_runs"]) <= allowed,
              "changed skill or citation")
        check((skill["stage"], content["parent"]) == (("revision", root) if index else ("formation", None)),
              "invalid skill lineage")
        if index:
            check(refs.intersection(failures), "revision does not cite an applied failure")

    def check_pairs(pairs, expected_slots, old=None):
        slots = set()
        for r in pairs:
            task_id, b, c = r["task_id"], r["baseline"], r["candidate"]
            slot = task_id, r["repeat"]
            check(slot not in slots and (r["split"], r["source_group"]) ==
                  (rows[task_id]["split"], rows[task_id]["source_group"]), "wrong trial stratum or repeat")
            slots.add(slot)
            check(b["run"] != c["run"] and b["task"] == c["task"] == task_id, "unpaired outcome")
            check(b["applied"] in (None, old) and c["applied"] in (None, candidate), "unexpected skill version")
            behavior = skill_behavior(dict(baseline_applied_skill=b["applied"], candidate_applied_skill=c["applied"],
                                           candidate_route=c["route"]), candidate, {old} if old else set())
            check(r["skill_behavior"] == behavior, "unbound change in skill use")
        check(slots == expected_slots, "incomplete paired trial coverage")

    development = {k for k, r in rows.items() if r["split"] == "dev"}
    check_pairs(data["screen"], {(t, 0) for t in development} if skills else set())
    check_pairs(data["parent_child"], {(t, n) for t in protocol["anchors"] for n in range(protocol["anchor_repeats"])}
                if len(skills) == 2 else set(), root)
    def flat(r):
        return dict(r, baseline_pass=r["baseline"]["passed"], candidate_pass=r["candidate"]["passed"])
    if data["screen"]:
        stats = first_round_gate([flat(r) for r in data["screen"]])
        check(stats["still_possible"] == (data["final_gate"] is not None), "wrong early stop")
    check(data["published"] == bool(data["final_gate"] and data["final_gate"]["passed"]), "unearned publication")
    if data["final_gate"]:
        support = {learn[k]["task"] for skill in skills for k in skill["evidence_runs"]}
        check_pairs(data["final_gate"]["rows"], {(t, n) for t in development for n in range(2)} | {(t, 0) for t in support})
        final_rows = [flat(r) for r in data["final_gate"]["rows"]]
        check(gate_summary(final_rows, 2)["gate"]["passed"] == data["final_gate"]["passed"], "incorrect full gate")
    expected_tests = {(k, 0) for k, r in rows.items() if r["split"] == "test"} if data["published"] else set()
    check_pairs(data["holdout"], expected_tests)
    strata = []
    for f, point in zip(protocol["family_order"], data["curve"]):
        added = [flat(r) | {"family": f} for r in data["holdout"] if rows[r["task_id"]]["family"] == f]
        strata.extend(added)
        check(curve_point(strata, f, added) == point, "unsupported diversity gain")
    check((len(data["holdout"]), len(data["curve"])) == ((30, 3) if data["published"] else (0, 0)), "wrong holdout exposure")
    check(data["holdout_task_exposures"] == len(data["holdout"]), "unexpected model exposure")
    expected_anchors = protocol["anchors"] if data["outcome"] in ("screen_rejected", "proposal_rejected") else []
    check([a["task"] for a in data["oracle"]] == expected_anchors and
          all(a["applied"] == protocol["oracle"] and a["route"] == "forced" for a in data["oracle"]),
          "unbound evaluator-only control")
    return {"cases": len(rows), "skills": len(skills), "development_pairs": len(data["screen"]),
            "dev_repairs": {arm: sum(r[arm]["passed"] for r in data["screen"]) for arm in ("baseline", "candidate")},
            "dev_calls": {arm: sum(r[arm]["model_calls"] for r in data["screen"]) for arm in ("baseline", "candidate")},
            "published": data["published"], "holdout_pairs": len(data["holdout"])}


if __name__ == "__main__":
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("command", choices=("verify", "export", "calibrate"))
    cli.add_argument("--root", type=Path, default=Path("experiments/optional-control-001"))
    cli.add_argument("--result", type=Path, default=PUBLIC / "optional-control.json")
    cli.add_argument("--registration", type=Path, default=REGISTRATION)
    cli.add_argument("--image", default="python:3.12-slim")
    args = cli.parse_args()
    proto = read(args.registration)
    if args.command == "calibrate":
        image = subprocess.check_output(["docker", "image", "inspect", "--format", "{{.Id}}", args.image], text=True).strip()
        opts = Settings(_env_file=None, mode="mock", image=image)
        proofs = bounded_map(lambda item: calibrate(item, opts), catalog(), 4)
        recorded = [dict(r, image=proto["image"]) for r in proofs]
        check(digest({"manifest": proto["manifest"], "rows": recorded}) == proto["calibration"], "calibration differs")
        print("calibrated", len(proofs), "cases with zero model calls", image)
    else:
        uptake = read(PUBLIC / "behavior-uptake.json")
        check(digest(uptake) == proto["uptake_report"], "different historical uptake export")
        print("verified archived GLM pairs", verify_uptake(uptake), "uptake",
              f"{uptake['uptake_numerator']}/{uptake['uptake_denominator']}",
              "mean excess TV", round(uptake["mean_excess_tv"], 4))
        result = export(args.root, proto) if args.command == "export" else read(args.result)
        print(verify_public(result, proto))
        if args.command == "export":
            dump(args.result, result)
