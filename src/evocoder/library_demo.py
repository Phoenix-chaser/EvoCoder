"""Explicit, scripted two-family engineering fixture. Not a learning-quality benchmark."""
from __future__ import annotations

import json
from pathlib import Path

from evocoder.contracts import Action, LibraryRoute, Settings, Skill, Task, canonical, digest
from evocoder.dataset import fixture_task
from evocoder.evolution import RECEIPT_MARKER
from evocoder.library_stream import library_stream
from evocoder.model import Model
from evocoder.store import Store


class ControlledLibraryModel:
    def request(self, schema, prompt, data, run_id):
        if schema is LibraryRoute:
            issue = data["task"]["issue"]
            family = "integer" if "[integer fixture]" in issue else "text"
            chosen = next((s for s in data["skills"] if s["applicability"][0].startswith(family)), None)
            return LibraryRoute(decision="apply" if chosen else "skip", skill_id=chosen["id"] if chosen else None,
                                condition_indices=[0] if chosen else [], exclusion_indices=[],
                                task_quotes=[issue] if chosen else [], reason="Explicit scripted fixture selection")
        if schema is Skill:
            episodes = data["evidence"]["episodes"]
            family = "integer" if "[integer fixture]" in episodes[-1]["task"]["issue"] else "text"
            parent = data["parent"]
            return Skill(name=f"{family}-fixture-procedure", parent=parent["id"] if parent else None,
                         applicability=[f"{family} fixture contract"],
                         steps=(["explicit_none: preserve valid false values and convert accepted scalars"]
                                if parent else [f"Handle optional {family} input after inspecting the contract"]),
                         verification=["Run the public boundary tests"], exclusions=["Unsupported complex objects"],
                         counterexamples=["Zero and False must be preserved"] if parent else [],
                         evidence_refs=data["allowed_evidence_refs"],
                         change_summary=(RECEIPT_MARKER if "verified_learning_observations" in data else
                                         "Controlled fixture proposal from public execution; no model-quality claim"))
        if schema is not Action:
            raise ValueError("unsupported controlled fixture decision")
        if "[integer fixture]" not in data["task"]["issue"]:
            return Model.mock_action(data)
        tools = [h["action"]["tool"] for h in data["history"]]
        if "write" not in tools:
            robust = bool(data["skills"])
            code = "def parse(value):\n    return 0 if value is None else int(value)\n" if robust else (
                "def parse(value):\n    return int(value or '')\n")
            return Action(tool="write", path="numbers.py", content=code, reason="Controlled fixture patch")
        return Action(tool="finish" if "test" in tools else "test", reason="Controlled fixture verification")


def integer_task(name, split):
    def tests(values):
        return ("from numbers import parse\n" + "\n".join(
            f"assert parse({value!r}) == {expected!r}" for value, expected in values) + "\n")
    return Task(id=name, split=split, source_group=f"synthetic-{name}", repository=f"fixture/{name}",
                issue="[offline contract fixture] [integer fixture] Parse optional integer input; None means zero.",
                files={"numbers.py": "def parse(value):\n    return int(value)\n",
                       "public_tests.py": tests([(None, 0), ("12", 12), (0, 0)])}, editable=["numbers.py"],
                public_command=["python", "public_tests.py"],
                grader_files={"judge.py": tests([(None, 0), ("-7", -7), (True, 1)])},
                grader_command=["python", "judge.py"])


def run_demo(root: Path, image="python:3.12-slim") -> dict:
    if root.exists():
        raise ValueError("demo directory must be new")
    root.mkdir(parents=True)
    settings = Settings(_env_file=None, mode="mock", state_dir=root / "state", image=image)
    tasks = [fixture_task("learn-text", "learn", False), integer_task("learn-integer", "learn"),
             fixture_task("learn-text-boundary", "learn", True),
             fixture_task("dev-text-a", "dev", True, 1), fixture_task("dev-text-b", "dev", True, 2),
             integer_task("dev-integer-a", "dev"), integer_task("dev-integer-b", "dev"),
             fixture_task("test-text", "test", True, 3), integer_task("test-integer", "test")]
    bundle = root / "bundle"
    bundle.mkdir()
    manifest = {"schema": 1, "claim": "scripted engineering fixtures, not generalization evidence", "tasks": []}
    for task in tasks:
        (bundle / f"{task.id}.json").write_text(canonical(task.model_dump()) + "\n")
        manifest["tasks"].append({"id": task.id, "path": f"{task.id}.json", "hash": digest(task.model_dump())})
    path = bundle / "manifest.json"
    path.write_text(canonical(manifest) + "\n")
    report = library_stream(settings, Store(settings.state_dir), path, root / "report", with_final_test=True,
                            model=ControlledLibraryModel())
    (root / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    return report
