from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from evocoder.contracts import Settings, Task
from evocoder.comparison import prepare as prepare_comparison, execute as execute_comparison, prepare_continuation
from evocoder.dataset import create_fixtures, load_catalog, pack_task
from evocoder.evolution import FEEDBACK_POLICIES, Evolution
from evocoder.experiment import demo
from evocoder.library import SkillLibrary
from evocoder.library_stream import library_stream
from evocoder.runtime import Runner
from evocoder.realworld import prepare as prepare_realworld, verify as verify_realworld
from evocoder.revision_diagnostic import execute as execute_diagnostic, prepare as prepare_diagnostic, prepare_bounded
from evocoder.sandbox import doctor
from evocoder.store import Store, nanos
from evocoder.stream import learn_stream


def parser():
    p = argparse.ArgumentParser(description="Execution-feedback-driven coding skill revision")
    p.add_argument("--mode", choices=["mock", "live"])
    p.add_argument("--env-file", type=Path, help="explicit provider configuration, separate from the default .env")
    p.add_argument("--state", type=Path)
    p.add_argument("--ledger", type=Path, help="original shared ledger when inspecting an isolated experiment arm")
    p.add_argument("--skill-routing", choices=["off", "semantic"])
    p.add_argument("--recover-actions", action=argparse.BooleanOptionalAction, default=None)
    commands = p.add_subparsers(dest="command", required=True)
    commands.add_parser("doctor")
    commands.add_parser("budget")
    route_prepare = commands.add_parser("route-study-prepare")
    route_prepare.add_argument("--previous", type=Path, required=True)
    route_prepare.add_argument("--audit", type=Path, required=True)
    route_prepare.add_argument("--output", type=Path, required=True)
    route_run = commands.add_parser("route-study-run")
    route_run.add_argument("--output", type=Path, required=True)
    grant = commands.add_parser("budget-grant", help="Record an explicitly authorized additional budget")
    grant.add_argument("--id", required=True)
    grant.add_argument("--additional", required=True)
    grant.add_argument("--expected-cap", required=True)
    grant.add_argument("--reason", required=True)
    comparison = commands.add_parser("comparison-prepare")
    comparison.add_argument("--manifest", type=Path, required=True)
    comparison.add_argument("--verification", type=Path, required=True)
    comparison.add_argument("--output", type=Path, required=True)
    comparison.add_argument("--repeats", type=int, default=3)
    comparison.add_argument("--workers", type=int, default=4)
    comparison.add_argument("--history-from", type=Path)
    comparison.add_argument("--seed-from", type=Path)
    comparison.add_argument("--replication-from", type=Path)
    comparison.add_argument("--pilot-seed", help="predeclared seed task in a small single-repository comparison")
    comparison.add_argument(
        "--carry-reservations-from", type=Path, action="append",
        help="stopped comparison that accounts for unresolved calls on this same ledger",
    )
    continuation = commands.add_parser("comparison-continue-prepare", help="Audit and carry forward a pre-test HTTP 402/500 checkpoint")
    continuation.add_argument("--previous", type=Path, required=True)
    continuation.add_argument("--output", type=Path, required=True)
    run_comparison = commands.add_parser("comparison-run")
    run_comparison.add_argument("--output", type=Path, required=True)
    run_comparison.add_argument("--resume-test", action="store_true", help="dispatch only never-started final cells")
    diagnostic = commands.add_parser("revision-diagnostic-prepare", help="freeze the draft parent and follow-up tasks")
    diagnostic.add_argument("--previous", type=Path, required=True)
    diagnostic.add_argument("--output", type=Path, required=True)
    diagnostic.add_argument("--manifest", type=Path, help="new frozen cohort; requires --verification")
    diagnostic.add_argument("--verification", type=Path, help="original/reference verification for the new cohort")
    retry = diagnostic.add_mutually_exclusive_group()
    retry.add_argument("--bounded-retry", dest="retry_policy", action="store_const", const="bounded_public_tests",
                       help="reuse learning after a rejected candidate")
    retry.add_argument("--grounded-retry", dest="retry_policy", action="store_const", const="grounded_patch_tests",
                       help="require a patch- and test-grounded summary on a new candidate")
    diagnostic.add_argument("--grounding-check", type=Path, help="offline Click #3509 controls for --grounded-retry")
    run_diagnostic = commands.add_parser("revision-diagnostic-run", help="observe parent failures and compare drafts")
    run_diagnostic.add_argument("--output", type=Path, required=True)
    commands.add_parser("skills")
    upstream = commands.add_parser("realworld-prepare", help="Download pinned public upstream task snapshots")
    upstream.add_argument("--catalog", type=Path, default=Path("benchmarks/realworld/cases.json"))
    upstream.add_argument("--cache", type=Path, default=Path("work/archives"))
    upstream.add_argument("--output", type=Path, required=True)
    check = commands.add_parser("realworld-verify", help="Verify actual defects and references; no model calls")
    check.add_argument("--manifest", type=Path, required=True)
    check.add_argument("--image", required=True)
    check.add_argument("--output", type=Path, required=True)
    fixtures = commands.add_parser("fixtures")
    fixtures.add_argument("--output", type=Path, default=Path("experiments/fixtures"))
    demonstration = commands.add_parser("demo")
    demonstration.add_argument("--output", type=Path)
    stream = commands.add_parser("stream")
    stream.add_argument("--manifest", type=Path, required=True)
    stream.add_argument("--output", type=Path, required=True)
    stream.add_argument("--max-proposals", type=int, default=3)
    stream.add_argument("--with-final-test", action="store_true")
    stream.add_argument("--feedback-policy", choices=FEEDBACK_POLICIES,
                        default="post_edit_or_budget")
    bank = commands.add_parser("library-stream", help="Learn multiple bounded skill families")
    bank.add_argument("--manifest", type=Path, required=True)
    bank.add_argument("--output", type=Path, required=True)
    bank.add_argument("--max-proposals", type=int, default=3)
    bank.add_argument("--initial-skills", nargs="+", default=[])
    bank.add_argument("--with-final-test", action="store_true")
    bank.add_argument("--feedback-policy", choices=FEEDBACK_POLICIES,
                      default="post_edit_or_budget")
    commands.add_parser("library")
    bank_eval = commands.add_parser("library-evaluate")
    bank_eval.add_argument("candidate")
    bank_eval.add_argument("--retain-parent", action="store_true",
                           help="Evaluate a portfolio containing both the candidate and its direct parent")
    bank_eval.add_argument("--manifest", type=Path, required=True)
    bank_activate = commands.add_parser("library-activate")
    bank_activate.add_argument("library_id")
    bank_activate.add_argument("--receipt", required=True)
    bank_activate.add_argument("--expected", required=True)
    bank_rollback = commands.add_parser("library-rollback")
    bank_rollback.add_argument("--to", default="none")
    bank_rollback.add_argument("--expected", required=True)
    run = commands.add_parser("run")
    run.add_argument("task", type=Path)
    run.add_argument("--skill", help="verified skill, or candidate on learn with --candidate")
    run.add_argument("--candidate", action="store_true")
    run.add_argument("--no-skill", action="store_true")
    run.add_argument("--library", help="published library ID, or 'active' for the published portfolio")
    inspect = commands.add_parser("inspect")
    inspect.add_argument("run_id")
    propose = commands.add_parser("propose")
    propose.add_argument("--runs", nargs="+", required=True)
    propose.add_argument("--parent")
    propose.add_argument("--proposal-policy", choices=["grounded_patch_tests", "grounded_scope", "bound_procedure"])
    propose.add_argument("--manifest", type=Path, help="original task snapshots for archived learning runs")
    propose.add_argument("--feedback-policy", choices=FEEDBACK_POLICIES,
                         default="post_edit_or_budget")
    evaluate = commands.add_parser("evaluate")
    evaluate.add_argument("skill_id")
    evaluate.add_argument("--manifest", type=Path, required=True)
    activate = commands.add_parser("activate")
    activate.add_argument("skill_id")
    activate.add_argument("--receipt", required=True)
    activate.add_argument("--expected", default="none")
    rollback = commands.add_parser("rollback")
    rollback.add_argument("--to", default="none")
    rollback.add_argument("--receipt")
    rollback.add_argument("--expected", required=True)
    pack = commands.add_parser("pack")
    pack.add_argument("--repo", type=Path, required=True)
    pack.add_argument("--issue", type=Path, required=True)
    pack.add_argument("--editable", nargs="+", required=True)
    pack.add_argument("--public-command", required=True, help="JSON argv array")
    pack.add_argument("--grader-dir", type=Path, required=True)
    pack.add_argument("--grader-command", required=True, help="JSON argv array")
    pack.add_argument("--id", required=True)
    pack.add_argument("--repository", required=True)
    pack.add_argument("--source-group", required=True)
    pack.add_argument("--split", choices=["learn", "dev", "test"], required=True)
    pack.add_argument("--output", type=Path, required=True)
    return p


def main():
    args = parser().parse_args()
    updates = {}
    if args.mode:
        updates["mode"] = args.mode
    if args.state:
        updates["state_dir"] = args.state
    if args.skill_routing is not None:
        updates["skill_routing"] = args.skill_routing
    if args.recover_actions is not None:
        updates["recover_actions"] = args.recover_actions
    settings = (Settings.from_env_file(args.env_file) if args.env_file else Settings()).model_copy(update=updates)
    try:
        settings.validate_live()
        store = Store(settings.state_dir, ledger=Store(args.ledger, unit=settings.accounting_unit) if args.ledger else None,
                      unit=settings.accounting_unit)
        command = args.command
        code = 0
        if command == "doctor":
            result = doctor(settings)
            code = int(not result["docker_ready"] or not result["image_ready"])
        elif command == "budget":
            result = store.budget()
        elif command == "route-study-prepare":
            from evocoder.route_study import prepare
            protocol = prepare(settings, args.previous, args.audit, args.output)
            result = {k: protocol[k] for k in ("parent", "child", "tasks", "repeats", "budget_before")}
        elif command == "route-study-run":
            from evocoder.route_study import execute
            report = execute(settings, args.output)
            result = {k: report.get(k) for k in ("completed", "phase", "new_cost_credits", "error")}
            code = 0 if report["completed"] else 2
        elif command == "budget-grant":
            store.grant_budget(args.id, nanos(args.additional), nanos(args.expected_cap), args.reason)
            result = store.budget()
        elif command == "comparison-prepare":
            result = prepare_comparison(settings, args.manifest, args.verification, args.output,
                                        repeats=args.repeats, workers=args.workers, history_from=args.history_from,
                                        seed_from=args.seed_from, replication_from=args.replication_from,
                                        carry_reservations_from=args.carry_reservations_from,
                                        pilot_seed=args.pilot_seed)
        elif command == "comparison-continue-prepare":
            result = prepare_continuation(settings, args.previous, args.output)
        elif command == "comparison-run":
            report = execute_comparison(settings, args.output, resume_test=args.resume_test)
            result = {k: report.get(k) for k in ("completed", "phase", "error", "test_preflight", "new_cost_usd", "new_cost_credits", "statistics")}
            code = 0 if report["completed"] else 2
        elif command == "revision-diagnostic-prepare":
            if bool(args.grounding_check) != (args.retry_policy == "grounded_patch_tests"):
                raise ValueError("--grounded-retry and --grounding-check must be used together")
            if args.retry_policy:
                if args.manifest is None or args.verification is None:
                    raise ValueError("a retry requires a verified cohort")
                protocol = prepare_bounded(
                    settings, args.previous, args.output, manifest=args.manifest, verification=args.verification,
                    proposal_policy=args.retry_policy, grounding_check=args.grounding_check,
                )
            else:
                protocol = prepare_diagnostic(
                    settings, args.previous, args.output, manifest=args.manifest, verification=args.verification,
                )
            result = {key: protocol.get(key) for key in (
                "parent", "follow_up_tasks", "dev_tasks", "regression_tasks", "test_tasks", "budget_before",
            )}
        elif command == "revision-diagnostic-run":
            report = execute_diagnostic(settings, args.output)
            result = {k: report.get(k) for k in (
                "completed", "phase", "outcome", "follow_up", "proposal_attempts", "candidate", "holdout", "error",
            )}
            code = 0 if report["completed"] else 2
        elif command == "fixtures":
            result = create_fixtures(args.output)
        elif command == "realworld-prepare":
            result = prepare_realworld(args.catalog, args.output, args.cache)
        elif command == "realworld-verify":
            receipt = verify_realworld(args.manifest, settings.model_copy(update={"image": args.image}), args.output)
            result = {"passed": receipt["passed"], "output": str(args.output),
                      "rows": [{"id": r["id"], "passed": r["passed"]} for r in receipt["rows"]]}
            code = int(not receipt["passed"])
        elif command == "demo":
            report = demo(settings, args.output)
            result = {"mode": report["mode"], "claim": report["claim"], "output": report["output"],
                      "v1": report["v1"], "v2": report["v2"], "active": report["active"],
                      "gate_passed": report["evaluation"]["gate"]["passed"],
                      "test_passed": sum(r["result"]["passed"] for r in report["test_runs"])}
        elif command == "stream":
            report = learn_stream(settings, store, args.manifest, args.output,
                                  args.max_proposals, args.with_final_test, feedback_policy=args.feedback_policy)
            result = {"mode": report["mode"], "claim": report["claim"],
                      "output": str(args.output.resolve()), "active": report["active"],
                      "proposal_count": report["proposal_count"], "timeline": report["timeline"],
                      "final_passed": sum(r["result"]["passed"] for r in report["final_runs"])}
            code = 0 if report["completed"] else 2
        elif command == "library-stream":
            report = library_stream(settings, store, args.manifest, args.output,
                                    max_proposals=args.max_proposals, with_final_test=args.with_final_test,
                                    initial_skills=args.initial_skills, feedback_policy=args.feedback_policy)
            result = {k: report[k] for k in ("completed", "error", "active_library", "proposal_count", "timeline")}
            result["output"] = str(args.output.resolve())
            code = 0 if report["completed"] else 2
        elif command == "library":
            active = store.active_library()
            result = {"active_library": active, "members": SkillLibrary(settings, store).members(active)}
        elif command == "library-evaluate":
            tasks = load_catalog(args.manifest)
            result = SkillLibrary(settings, store).evaluate(args.candidate, [t for t in tasks if t.split == "dev"],
                                                           [t for t in tasks if t.split == "learn"],
                                                           retain_parent=args.retain_parent)
            code = int(not result["gate"]["passed"])
        elif command in {"library-activate", "library-rollback"}:
            target = args.library_id if command == "library-activate" else args.to
            store.activate_library(None if target == "none" else target, getattr(args, "receipt", None),
                                   None if args.expected == "none" else args.expected, mode=settings.mode,
                                   rollback=command == "library-rollback")
            result = {"active_library": store.active_library()}
        elif command == "run":
            task = Task.model_validate_json(args.task.read_text())
            if sum(bool(x) for x in (args.no_skill, args.skill, args.library)) > 1:
                raise ValueError("choose one of --no-skill, --skill or --library")
            if task.split == "learn":
                store.artifact("learn_task_snapshot", task.model_dump())
            if args.library:
                key = store.active_library() if args.library == "active" else args.library
                if key is None:
                    raise ValueError("no published library; use --no-skill explicitly")
                run_id = Runner(settings, store).solve(task, use_active=False, library_id=key)
            else:
                run_id = Runner(settings, store).solve(task, args.skill,
                                                      use_active=not args.no_skill and not args.skill,
                                                      candidate=args.candidate)
            result = store.run(run_id)
            code = int(not result["result"]["passed"])
        elif command == "inspect":
            result = {"run": store.run(args.run_id), "events": store.events(args.run_id)}
        elif command == "propose":
            snapshots = ({t.id: t for t in load_catalog(args.manifest) if t.split == "learn"}
                         if args.manifest else None)
            key = Evolution(settings, store, feedback_policy=args.feedback_policy,
                            grounding_tasks=snapshots, proposal_policy=args.proposal_policy).propose(args.runs, args.parent)
            result = {"skill_id": key, "skill": store.get_artifact(key)}
        elif command == "evaluate":
            tasks = load_catalog(args.manifest)
            with store.connect() as db:
                observed = {r[0] for r in db.execute(
                    "SELECT DISTINCT task_id FROM runs WHERE split='learn' AND purpose='solve' AND mode=?",
                    (settings.mode,))}
            result = Evolution(settings, store).evaluate(args.skill_id,
                                                        [t for t in tasks if t.split == "dev"],
                                                        [t for t in tasks if t.split == "learn" and t.id in observed])
            code = int(not result["gate"]["passed"])
        elif command == "skills":
            result = {"active": store.active(), "lineage": Evolution(settings, store).lineage()}
        elif command in {"activate", "rollback"}:
            target = args.skill_id if command == "activate" else args.to
            target = None if target == "none" else target
            store.activate(target, args.receipt, None if args.expected == "none" else args.expected,
                           reason=command)
            result = {"active": store.active()}
        elif command == "pack":
            if args.output.exists():
                raise ValueError("output task already exists; frozen tasks cannot be overwritten")
            task = pack_task(args.repo, args.issue, args.editable, json.loads(args.public_command),
                             args.grader_dir, json.loads(args.grader_command), args.id, args.split,
                             args.source_group, args.repository)
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(task.model_dump_json(indent=2) + "\n")
            result = {"path": str(args.output), "id": task.id}
        else:
            raise ValueError("unsupported command")
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return code
    except Exception as exc:
        message = str(exc)
        if settings.api_key:
            message = message.replace(settings.api_key, "[redacted]")
        print(json.dumps({"error": type(exc).__name__, "message": message}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
