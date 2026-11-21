import io
import tarfile

import pytest

from evocoder.contracts import Action
from evocoder.realworld import RealCase, archive_files, build_task


def tar_body(items):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for path, body, kind in items:
            info = tarfile.TarInfo(path)
            info.type = kind
            info.size = len(body) if kind == tarfile.REGTYPE else 0
            archive.addfile(info, io.BytesIO(body) if info.size else None)
    return buffer.getvalue()


def test_upstream_archive_never_extracts_traversal_or_links():
    with pytest.raises(ValueError):
        archive_files(tar_body([("repo/../escape", b"x", tarfile.REGTYPE)]))
    assert archive_files(tar_body([("repo/link", b"", tarfile.SYMTYPE)])) == {}


def test_task_is_pristine_source_and_reference_stays_controller_only():
    case = RealCase(id="example", repository="team/example", split="learn", pull_request=1,
                    base_commit="a" * 40, fixed_commit="b" * 40, package="example",
                    issue="A real upstream defect description", public_tests=["tests/test_unit.py"],
                    regression_tests=["tests/test_unit.py"], regression_filter="regression",
                    reproducer="repro.py")
    base = {"src/example/__init__.py": "BROKEN = True\n", "tests/test_unit.py": "def test_existing(): pass\n",
            "LICENSE": "Example license", "CHANGELOG.md": "do not leak", ".env": "do not leak"}
    fixed = {**base, "src/example/__init__.py": "BROKEN = False\n",
             "tests/test_unit.py": "def test_regression(): pass\n"}
    task, reference, provenance = build_task(case, base, fixed, "def test_repro(): pass\n")
    assert task.files["src/example/__init__.py"] == base["src/example/__init__.py"]
    assert task.files["tests/test_unit.py"] != fixed["tests/test_unit.py"]
    assert "False" in reference["src/example/__init__.py"]
    assert "CHANGELOG.md" not in task.files and ".env" not in task.files
    assert task.files["LICENSE"] == "Example license"
    assert "regression" in task.grader_files["upstream_regression/test_unit.py"]
    assert provenance["human_selected_and_adapted"]
    assert "PYTHONPATH=/workspace/src" in task.public_command


def test_edit_contract_requires_actual_replacement():
    with pytest.raises(ValueError):
        Action(tool="edit", path="src/a.py", old_text="", new_text="x", reason="bad")
    with pytest.raises(ValueError):
        Action(tool="read", path="src/a.py", start_line=0, reason="bad")


def test_missing_diagnostic_reason_is_marked_without_inventing_a_rationale():
    action = Action(tool="read", path="src/a.py")
    assert action.reason == "[model omitted action rationale]"
    assert "reason" not in action.model_fields_set
