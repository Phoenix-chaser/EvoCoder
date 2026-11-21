from __future__ import annotations

import json
from pathlib import Path

from evocoder.contracts import Task, canonical, digest, safe_path


def fixture_task(name: str, split: str, scalar: bool, suffix: int = 0) -> Task:
    """Synthetic contract fixtures; shared generator means no independence/generalization claim."""
    package = f"module_{suffix}"
    function = ["normalize", "canonicalize", "format_value", "clean"][suffix % 4]
    source = f"{package}/text.py"
    cases = [(None, ""), (" A b ", "a b"), ("", "")]
    if scalar:
        cases.extend([(0, "0"), (False, "false"), (42, "42")])

    def test_file(values):
        return (f"from {package}.text import {function}\nimport unittest\n"
                "class Contract(unittest.TestCase):\n"
                "    def test_contract(self):\n"
                f"        for value, expected in {values!r}:\n"
                "            with self.subTest(value=value):\n"
                f"                self.assertEqual({function}(value), expected)\n"
                "if __name__ == '__main__': unittest.main()\n")

    hidden_cases = [*cases, ("\tXYZ\n", "xyz"), (" Re Pe At ", "re pe at")]
    if scalar:
        hidden_cases.extend([(-2, "-2"), (True, "true")])
    return Task(id=name, source_group=f"synthetic-{name}", repository=f"fixture/{name}", split=split,
                issue=("[offline contract fixture] Fix optional input normalization: strip surrounding "
                       "whitespace and lowercase. None means empty text. "
                       + ("Accept text, integer and boolean values, preserving valid falsy inputs."
                          if scalar else "The supported input is text or None.")),
                files={f"{package}/__init__.py": "", source:
                       f"def {function}(value):\n    return value.strip().lower()\n",
                       "public_tests.py": test_file(cases),
                       "README.md": "Small synthetic repository. Run python public_tests.py.\n"},
                editable=[source], public_command=["python", "public_tests.py"],
                grader_files={"judge_contract.py": test_file(hidden_cases)},
                grader_command=["python", "judge_contract.py"])


def create_fixtures(root: Path) -> dict:
    root.mkdir(parents=True, exist_ok=True)
    tasks = []
    for split, count in [("learn", 4), ("dev", 4), ("test", 4)]:
        for i in range(count):
            scalar = i > 0 if split == "learn" else i % 2 == 0
            task = fixture_task(f"{split}-{i + 1:02d}", split, scalar, i)
            path = root / f"{task.id}.json"
            content = canonical(task.model_dump()) + "\n"
            if path.exists() and path.read_text() != content:
                raise ValueError("fixture destination already contains a different frozen task")
            path.write_text(content)
            tasks.append({"path": path.name, "id": task.id, "split": split,
                          "source_group": task.source_group, "hash": digest(task.model_dump())})
    manifest = {"schema": 1, "kind": "synthetic_engineering_fixtures",
                "generator": "optional-input-v1", "claim": "not research or generalization evidence",
                "tasks": tasks}
    (root / "manifest.json").write_text(canonical(manifest) + "\n")
    return {"manifest_hash": digest(manifest), **manifest}


def load_catalog(manifest_path: Path) -> list[Task]:
    manifest = json.loads(manifest_path.read_text())
    tasks, ids, groups = [], set(), {}
    for entry in manifest["tasks"]:
        task = Task.model_validate_json((manifest_path.parent / safe_path(entry["path"])).read_text())
        if digest(task.model_dump()) != entry["hash"]:
            raise ValueError("dataset task hash changed after freezing")
        if task.id in ids:
            raise ValueError("duplicate task id")
        ids.add(task.id)
        if task.source_group in groups and groups[task.source_group] != task.split:
            raise ValueError("source group crosses dataset split")
        groups[task.source_group] = task.split
        tasks.append(task)
    return tasks


def pack_task(repo: Path, issue: Path, editable: list[str], public_command: list[str],
              grader_dir: Path, grader_command: list[str], task_id: str, split: str,
              source_group: str, repository: str) -> Task:
    """Import local trusted task assets into a frozen snapshot; no repository execution."""
    def snapshot(root):
        root = root.resolve()
        files = {}
        for path in sorted(root.rglob("*")):
            relative = path.relative_to(root)
            if any(p.startswith(".") or p in {"node_modules", "__pycache__", "venv"}
                   for p in relative.parts):
                continue
            if path.is_symlink():
                raise ValueError("task snapshots cannot contain symlinks")
            if path.is_file():
                files[safe_path(relative.as_posix())] = path.read_text()
        return files
    if grader_dir.resolve().is_relative_to(repo.resolve()):
        raise ValueError("grader directory must be outside solver repository")
    return Task(id=task_id, source_group=source_group, repository=repository, split=split,
                issue=issue.read_text(), files=snapshot(repo), editable=editable,
                public_command=public_command, grader_files=snapshot(grader_dir),
                grader_command=grader_command)
