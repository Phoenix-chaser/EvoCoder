"""Run the frozen parent and child with the same live switch feedback and original 16-call budget."""
import argparse
import fcntl
from pathlib import Path

from evocoder.comparison import dump, frozen_knowledge, sources
from evocoder.contracts import Settings, digest
from evocoder.library import SkillLibrary
from evocoder.revision_diagnostic import check_fresh_holdouts, copy_closed_seed, freeze_protocol, read
from evocoder.runtime import validation
from evocoder.store import Store
from evocoder.switch_feedback import axis_from_public
from prepare_click_procedure import GRADER, PYTEST
from prepare_switch_learning import off_mutant
from run_click_procedure import TrialRunner, check_learning, compare, options_for, paired_gate, pick
from verify_authored_cli_checks import calibrate, entries
from verify_switch_trials import verify_trial


def drivers():
    names = ("run_switch_study", "verify_switch_trials", "prepare_switch_learning", "run_click_procedure",
             "prepare_click_procedure", "prepare_click_spacing", "verify_frozen_benchmark", "verify_candidates",
             "verify_authored_cli_checks", "verify_cli_reach", "continue_route_study", "probe_click_route_patches")
    return {n: Path(__file__).with_name(n + ".py").read_text() for n in names}


def settings_for(settings, origin):
    opts = options_for(settings, read(origin / "protocol.json")["settings"], missing=True)
    if opts.max_steps != 16:
        raise ValueError("original total budget must be 16")
    return opts.model_copy(update={"live_switch_feedback": True})


def prepare(settings, origin, learn, root):
    previous, result = read(origin / "protocol.json"), read(origin / "result.json")
    opts = settings_for(settings, origin)
    ledger = Store(opts.state_dir, unit=opts.accounting_unit)
    if not result["completed"] or ledger.budget()["blocked"] or root.exists():
        raise ValueError("expected closed predecessor, usable shared ledger and new destination")
    ids = [row["id"] for row in read(learn)["tasks"]]
    subjects = pick(learn, ids)
    proof = check_learning(list(subjects.values()), learn, opts.image, origin.parent, missing=True)
    independent = read(learn.parent / "independence.json")
    if not independent["passed"] or independent["manifest_hash"] != digest(read(learn)):
        raise ValueError("independence evidence is stale")
    check_fresh_holdouts(list(subjects.values()), origin.parent, ledger, label="learning")
    bench = Path(previous["manifest"])
    controls = calibrate(learn, bench, opts.image, mutant_for=off_mutant)
    # Measure the *single supplied* example on the reference before dispatch. A private
    # witness separately checks the monitor. Neither its source nor results enter solver inputs.
    bound = entries(learn, ids) | entries(bench, previous["focus"])
    for key, (task, ref) in bound.items():
        axis = axis_from_public(task.files, task.public_command, task.issue)
        tests = {"public": (task.public_command, {**task.files, **ref}, [True])}
        if key in ids:
            tests["private_off"] = ([*PYTEST, GRADER], {**task.files, **ref, GRADER: task.grader_files[GRADER]}, [False])
        for name, (argv, files, expected) in tests.items():
            o = validation(files, argv, opts, "observer-control", axis=axis)
            measured = o.get("switch_observation", {}).get("modes", [])
            if (o["exit_code"] or o["image_id"] != opts.image or not set(expected) <= set(measured)
                    or (key in ids and measured != expected)):
                raise ValueError(f"public switch instrumentation failed calibration for {key}/{name}: {o.get('switch_observation')}")
            controls["tasks"][key].setdefault("monitor", {})[name] = o["switch_observation"]
    st = copy_closed_seed(origin, root, ledger)
    parent, child = previous["parent"], result["candidate"]
    lib = SkillLibrary(opts, st)
    if st.get_artifact(child)["parent"] != parent or st.active_library() or st.active():
        raise ValueError("expected an unpublished direct parent and child")
    support = lib.provenance(child)["support"]
    manifests = {**previous["support_manifests"], previous["learn_manifest"]: previous["learn_hash"],
                 previous["manifest"]: previous["manifest_hash"]}
    with ledger.connect() as db:
        cursor = db.execute("SELECT coalesce(max(rowid),0) FROM calls").fetchone()[0]
    driver = drivers()
    dump(root / "drivers.json", driver)
    dump(root / "controls.json", controls)
    return freeze_protocol(root, {
        "claim": "in_budget_switch_feedback", "origin": str(origin), "origin_hash": digest(result),
        "settings": opts.public(), "parent": parent, "child": child, "manifest": str(bench), "manifests": manifests,
        "learn_manifest": str(learn), "learn_hash": digest(read(learn)), "learn_proof": digest(proof),
        "independence_hash": digest(independent), "controls_hash": digest(controls), "drivers_hash": digest(driver),
        "learn_ids": ids, "support": support, **{k: previous[k] for k in ("dev", "focus", "sentinels", "holdouts")},
        "budget_before": ledger.budget(), "previous_billing_row": cursor, "knowledge": frozen_knowledge(st),
        "policy": {"learning": "one normal paired trial on each new enabled-only case; alternate first arm by task",
                   "feedback": "identical prompt, observed invocations, patch-scoped history and budget in both arms",
                   "assertion": "in-budget model source, matched raw action, same actually reached OFF invocation in three trees, "
                                "passing on action patch and reference, failing at an authored assert on calibrated OFF-only mutant",
                   "focus": "normal routing; two counterbalanced paired repeats on count and #557",
                   "gate": previous["policy"]["gate"], "holdout": previous["policy"]["holdout"],
                   "publication": False, "feedback_off_comparator": False}})


def execute(settings, root):
    p = read(root / "protocol.json")
    origin, learn, bench = Path(p["origin"]), Path(p["learn_manifest"]), Path(p["manifest"])
    opts = settings_for(settings, origin)
    if (opts.public() != p["settings"] or digest(sources()) != p["source_hash"] or digest(drivers()) != p["drivers_hash"]
            or digest(read(root / "drivers.json")) != p["drivers_hash"] or digest(read(root / "controls.json")) != p["controls_hash"]
            or digest(read(origin / "result.json")) != p["origin_hash"] or digest(read(learn)) != p["learn_hash"]
            or digest(read(learn.parent / "verification.json")) != p["learn_proof"]
            or digest(read(learn.parent / "independence.json")) != p["independence_hash"]
            or any(digest(read(Path(m))) != h for m, h in p["manifests"].items())):
        raise ValueError("changed frozen experiment")
    ledger = Store(opts.state_dir, unit=opts.accounting_unit)
    with (ledger.root / "comparison.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        st = Store(root / "seed", ledger=ledger)
        if (root / "result.json").exists() and read(root / "result.json")["completed"]:
            return read(root / "result.json")
        if not (root / "started.json").exists():
            if ledger.budget() != p["budget_before"]:
                raise ValueError("shared budget changed before dispatch")
            dump(root / "started.json", {"protocol": digest(p)})
        if read(root / "started.json") != {"protocol": digest(p)} or frozen_knowledge(st) != p["knowledge"]:
            raise ValueError("study identity or frozen skills changed")
        r = {"completed": False, "phase": "learning", "learning": [], "rows": [], "gate": {"status": "pending", "rows": []},
             "holdout": {"status": "not_run", "rows": []}, "protocol": digest(p)}
        runner = TrialRunner(opts, st, root, p)
        lib = SkillLibrary(opts, st, runner)
        controlled = entries(learn, p["learn_ids"]) | entries(bench, p["focus"])

        def observe(task, key):
            path = root / "assertions" / (key + ".json")
            receipt = verify_trial(st, key, task, controlled[task.id][1], opts)
            dump(path, receipt)
            return digest(receipt)

        try:
            for idx, key in enumerate(p["learn_ids"]):
                task = controlled[key][0]
                row = compare(lib, runner, task, 0, "learning", bool(idx % 2), p["parent"], p["child"])
                for arm in ("baseline", "candidate"):
                    row[arm + "_behavior_hash"] = observe(task, row[arm + "_run"])
                r["learning"].append(row)
                dump(root / "progress.json", r)
            tasks = pick(bench, [*p["dev"], *p["sentinels"]])
            for path in p["manifests"]:
                m = Path(path)
                tasks.update(pick(m, [x["id"] for x in read(m)["tasks"] if x["id"] in p["support"]]))
            if any(digest(tasks[k].model_dump()) != h for k, h in p["support"].items()):
                raise ValueError("support snapshot changed")
            paired_gate(p, r, root, st, tasks, sorted(p["support"]), lib, runner, p["parent"], p["child"], observe)
            if frozen_knowledge(st) != p["knowledge"]:
                raise ValueError("skills changed during evaluation")
            r.update(completed=True, phase="complete")
        finally:
            with ledger.connect() as db:
                ids = {row[0] for row in db.execute("SELECT DISTINCT run_id FROM calls WHERE rowid>?", (p["previous_billing_row"],))}
            allowed = {a["run_id"] for slot in runner.slots.values() for a in slot}
            r["costs"] = st.costs(sorted(allowed))
            after = ledger.budget()
            r["budget_after"] = after
            for k in ("spent", "reserved"):
                actual = r["costs"][("cost" if k == "spent" else k) + "_credits"]
                if abs(after[k + "_credits"] - p["budget_before"][k + "_credits"] - actual) > 1e-6 or ids - allowed:
                    r.update(completed=False, error="shared ledger attribution mismatch")
            dump(root / "result.json", r)
        return r


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("command", choices=["prepare", "run"])
    ap.add_argument("--origin", type=Path, default=Path("experiments/glm-click-missing-state-001"))
    ap.add_argument("--learn", type=Path, default=Path("experiments/click-switch-feedback-learn-001/manifest.json"))
    ap.add_argument("--root", type=Path, default=Path("experiments/glm-click-switch-feedback-001"))
    ap.add_argument("--env-file", type=Path, default=Path(".env.glm"))
    args = ap.parse_args()
    settings = Settings.from_env_file(args.env_file)
    if args.command == "prepare":
        protocol = prepare(settings, args.origin.resolve(), args.learn.resolve(), args.root.resolve())
        print("frozen", digest(protocol), flush=True)
    else:
        result = execute(settings, args.root.resolve())
        print(result.get("outcome"), result["costs"], flush=True)
        raise SystemExit(0 if result["completed"] else 1)
