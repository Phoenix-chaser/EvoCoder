"""Bounded skill families with whole-library regression and atomic publication."""

from __future__ import annotations

from threading import Event

from evocoder.contracts import Settings, Skill, Task, canonical, digest
from evocoder.gate import gate_summary, skill_behavior
from evocoder.jobs import bounded_map
from evocoder.repeat_gate import DEV_REPEATS, SUPPORT_REPEATS, repeated_gate
from evocoder.runtime import Runner
from evocoder.store import Store


class SkillLibrary:
    def __init__(self, settings: Settings, store: Store, runner=None):
        if settings.skill_routing != "semantic":
            raise ValueError("skill libraries require semantic routing")
        self.settings, self.store = settings, store
        self.runner = runner or Runner(settings, store)

    def provenance(self, key: str) -> dict:
        """Check complete lineage, mode and learn-only evidence before routing/evaluation."""
        seen, required, groups = set(), {}, set()
        current, root = key, None
        while current:
            if current in seen or len(seen) >= 100:
                raise ValueError("invalid skill ancestry")
            seen.add(current)
            skill = Skill.model_validate(self.store.get_artifact(current))
            with self.store.connect() as db:
                row = db.execute("SELECT parent,evidence_hash FROM skills WHERE id=?", (current,)).fetchone()
            if row is None or row["parent"] != skill.parent:
                raise ValueError("missing or inconsistent proposal provenance")
            evidence = self.store.get_artifact(row["evidence_hash"])
            if not evidence.get("episodes") or evidence.get("parent_id") != skill.parent:
                raise ValueError("invalid frozen proposal evidence")
            for episode in evidence["episodes"]:
                run = self.store.run(episode["run_id"])
                if run["mode"] != self.settings.mode or run["split"] != "learn" or run["purpose"] != "solve":
                    raise ValueError("library requires same-mode learn/solve evidence")
                if run["state"] not in {"COMPLETE", "FAILED", "EXHAUSTED"}:
                    raise ValueError("library evidence must be completed")
                prior = required.setdefault(run["task_id"], run["task_hash"])
                if prior != run["task_hash"]:
                    raise ValueError("evidence reuses a task ID with different snapshots")
                start = next(e for e in episode["events"] if e["kind"] == "START")
                groups.add(start["payload"]["task"]["source_group"])
            root, current = current, skill.parent
        return {"root": root, "members": seen, "support": required, "groups": groups}

    def members(self, library_id: str | None) -> list[str]:
        return self.store.library_members(library_id, mode=self.settings.mode) if library_id else []

    def validate_family_pool(self, members: list[str]) -> None:
        """A routed pool can retain one direct parent with its specialization per family."""
        families = {}
        for key in members:
            families.setdefault(self.provenance(key)["root"], []).append(key)
        for versions in families.values():
            if len(versions) == 1:
                continue
            if len(versions) == 2 and any(self.store.get_artifact(v)["parent"] in versions for v in versions):
                continue
            raise ValueError("same-family alternatives must be a direct parent and child")

    def retain_parent(self, members: list[str], candidate: str) -> list[str]:
        parent = self.store.get_artifact(candidate)["parent"]
        if not parent:
            raise ValueError("parallel specialization requires a direct parent")
        updated = sorted({*members, parent, candidate})
        if len(updated) > 8:
            raise ValueError("library capacity reached; no automatic skill eviction")
        self.validate_family_pool(updated)
        return updated

    def replace(self, members: list[str], candidate: str, *, allow_retained=False) -> list[str]:
        roots = {key: self.provenance(key)["root"] for key in members}
        if len(set(roots.values())) != len(roots):
            if not allow_retained:
                raise ValueError("pool contains multiple versions of one family")
            self.validate_family_pool(members)
        provenance = self.provenance(candidate)
        new_root = provenance["root"]
        if any(roots[k] == new_root and k not in provenance["members"] for k in members):
            raise ValueError("replacement must descend from the current family version")
        updated = sorted([k for k in members if roots[k] != new_root] + [candidate])
        if len(updated) > 8:
            raise ValueError("library capacity reached; no automatic skill eviction")
        return updated

    def compare_pair(
        self, task: Task, repeat: int, baseline: list[str], variants: list[str], candidate: str,
        *, reverse: bool, stop: Event, fixed_route: str | None = None, match_solver_slots: bool = False,
    ) -> dict:
        pools = {"baseline": baseline, "candidate": variants}
        order = ("candidate", "baseline") if reverse else ("baseline", "candidate")
        runs = {}
        for arm in order:
            if stop.is_set():
                raise RuntimeError("another paired trial failed; stop further model attempts")
            intervention = {"fixed_route": fixed_route} if fixed_route is not None else {}
            if match_solver_slots and not pools[arm]:
                intervention["reserve_empty_route"] = True
            if arm == "baseline" and baseline and (active := self.store.active_library()):
                manifest = self.store.get_artifact(active)
                context = manifest.get("route_context", "issue")
                if sorted(baseline) == manifest["members"] and context != self.settings.library_route_context:
                    intervention["route_context"] = context
            run_id = self.runner.solve(task, use_active=False, purpose="evaluate", skill_ids=pools[arm],
                                       **intervention)
            run = self.store.run(run_id)
            if run["state"] in {"ERROR", "INTERRUPTED"}:
                stop.set()
                raise RuntimeError(f"paired evaluation error at {task.id}/{arm}, run {run_id}; stop without retry")
            runs[arm] = run
        b, c = runs["baseline"], runs["candidate"]
        row = {
            "task_id": task.id,
            "source_group": task.source_group,
            "split": task.split,
            "repeat": repeat,
            "task_hash": digest(task.model_dump()),
            "baseline_run": b["id"],
            "candidate_run": c["id"],
            "baseline_pass": b["result"]["passed"],
            "candidate_pass": c["result"]["passed"],
            "baseline_applied_skill": b["skill_id"],
            "candidate_applied_skill": c["skill_id"],
            "baseline_route": b["result"].get("routing", {}).get("decision"),
            "candidate_route": c["result"].get("routing", {}).get("decision"),
            "infrastructure_error": not b["result"].get("image_id")
            or b["result"].get("image_id") != c["result"].get("image_id"),
        }
        row["skill_behavior"] = skill_behavior(row, candidate, set(baseline) - set(variants))
        return row

    def evaluate(
        self, candidate: str, dev: list[Task], support: list[Task], *, workers=1,
        diagnostic_parent: str | None = None, regression: list[Task] | None = None, on_pair=None,
        retain_parent: bool = False, repeated: bool = False,
    ) -> dict:
        if not 1 <= workers <= 4:
            raise ValueError("gate workers must be 1..4")
        if repeated and self.settings.mode != "live":
            raise ValueError("the repeated evidence gate requires live model wire receipts")
        if len(dev) < 2 or any(t.split != "dev" for t in dev) or len({t.source_group for t in dev}) < 2:
            raise ValueError("library gate requires at least two independent dev groups")
        if any(t.split != "learn" for t in support):
            raise ValueError("library support must contain learn tasks only")
        regression = regression or []
        if regression and (diagnostic_parent is None or any(t.split != "dev" for t in regression)):
            raise ValueError("known regression tasks require a diagnostic and dev snapshots")
        dev_and_regression = [*regression, *dev]
        regression_ids = {t.id for t in regression}
        tasks = [*dev_and_regression, *support]
        if len({t.id for t in tasks}) != len(tasks):
            raise ValueError("duplicate task in library gate")
        baseline = self.store.active_library()
        if diagnostic_parent is None:
            old = self.members(baseline)
        else:
            skill = Skill.model_validate(self.store.get_artifact(candidate))
            if skill.parent != diagnostic_parent:
                raise ValueError("diagnostic requires the candidate's direct parent")
            old = [diagnostic_parent]
        parallel_baseline = bool(baseline and self.store.get_artifact(baseline)["schema"] == 2)
        new = (self.retain_parent(old, candidate) if retain_parent
               else self.replace(old, candidate, allow_retained=parallel_baseline))
        if old == new:
            raise ValueError("library candidate makes no change")
        # Replay every family's frozen learning evidence, including the replaced version.
        required, evidence_groups = {}, set()
        for key in set(old + new):
            provenance = self.provenance(key)
            for task_id, task_hash in provenance["support"].items():
                if required.setdefault(task_id, task_hash) != task_hash:
                    raise ValueError("conflicting support snapshots across skills")
            evidence_groups.update(provenance["groups"])
        supplied = {t.id: digest(t.model_dump()) for t in support}
        if any(supplied.get(key) != value for key, value in required.items()):
            raise ValueError("missing or changed full-library support replay")
        if any(
            t.source_group in evidence_groups or t.source_group in {s.source_group for s in support}
            for t in dev_and_regression
        ):
            raise ValueError("dev source group overlaps library learning evidence")
        target = None
        if diagnostic_parent is None:
            manifest = {"schema": 1, "kind": "skill_library", "mode": self.settings.mode, "members": new}
            if retain_parent or self.settings.library_route_context != "issue":
                manifest.update(schema=2, route_context=self.settings.library_route_context)
            target = self.store.artifact("skill_library", manifest)
        stop = Event()
        dev_repeats = DEV_REPEATS if repeated else (2 if self.settings.mode == "live" else 1)

        def compare(item):
            index, task, repeat = item
            row = self.compare_pair(
                task, repeat, old, new, candidate, reverse=(index + repeat) % 2 == 1, stop=stop,
                **({"match_solver_slots": True} if repeated else {}),
            )
            if repeated:
                for arm in ("baseline", "candidate"):
                    first = next((e["payload"] for e in self.store.events(row[arm + "_run"])
                                  if e["kind"] == "MODEL_RESPONSE" and e["payload"]["expected_function"] == "submit_action"), None)
                    if first is None:
                        raise ValueError("missing first solver request for repeated evaluation")
                    row[arm + "_request"] = first["request_body_hash"]
            if task.id in regression_ids:
                row["evaluation_role"] = "known_regression"
            if self.store.active_library() != baseline:
                stop.set()
                raise ValueError("library changed during evaluation; discard stale comparison")
            return row

        # Each repeat reverses the arm order on a given task.
        pairs = [
            (index, task, repeat)
            for repeat in range(dev_repeats) for index, task in enumerate(dev_and_regression)
        ]
        pairs.extend((index, task, repeat) for repeat in range(SUPPORT_REPEATS if repeated else 1)
                     for index, task in enumerate(support, start=len(dev_and_regression)))
        rows = bounded_map(compare, pairs, workers, on_result=on_pair)
        outcome = (repeated_gate(rows, {t.id: (t.split, t.source_group, digest(t.model_dump())) for t in tasks})
                   if repeated else gate_summary(rows, dev_repeats))
        report = {
            "schema": 2,
            "kind": "library_evaluation" if diagnostic_parent is None else "parent_child_diagnostic",
            "mode": self.settings.mode,
            "claim": "engineering_validation_only" if self.settings.mode == "mock" else "exploratory",
            "baseline_library": baseline if diagnostic_parent is None else None,
            "library_id": target,
            "candidate": candidate,
            "baseline_members": old,
            "members": new,
            "rows": rows,
            **outcome,
            "config_hash": digest(self.settings.public()),
            "costs": {
                arm: self.store.costs([r[f"{arm}_run"] for r in rows]) for arm in ("baseline", "candidate")
            },
        }
        if diagnostic_parent is not None:
            return {**report, "receipt": None}
        receipt = self.store.artifact("library_evaluation", report)
        with self.store.connect(write=True) as db:
            db.execute(
                "INSERT OR IGNORE INTO library_evaluations VALUES(?,?,?,?,?)",
                (receipt, target, baseline, canonical(report), int(outcome["gate"]["passed"])),
            )
        return {"receipt": receipt, **report}
