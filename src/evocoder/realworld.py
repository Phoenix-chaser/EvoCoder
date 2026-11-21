"""Pinned upstream snapshots, controller-only reference patches and offline verification."""
from __future__ import annotations

import io
import json
import tarfile
from pathlib import Path

import httpx
from pydantic import Field

from evocoder.contracts import Settings, Strict, Task, canonical, digest, safe_path
from evocoder.dataset import load_catalog
from evocoder.runtime import validation


class RealCase(Strict):
    id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,80}$")
    repository: str = Field(pattern=r"^[\w.-]+/[\w.-]+$")
    split: str = Field(pattern=r"^(learn|dev|test)$")
    pull_request: int = Field(gt=0)
    base_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    fixed_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    package: str = Field(pattern=r"^[a-zA-Z_]\w*$")
    issue: str = Field(min_length=10, max_length=16000)
    public_tests: list[str] = Field(min_length=1)
    regression_tests: list[str] = Field(min_length=1)
    regression_filter: str = Field(min_length=1)
    reproducer: str


def archive_files(body: bytes) -> dict[str, str]:
    """Read text without extracting archive paths or executing project/build hooks."""
    files, total, roots = {}, 0, set()
    with tarfile.open(fileobj=io.BytesIO(body), mode="r:gz") as archive:
        for item in archive:
            parts = item.name.split("/", 1)
            roots.add(parts[0])
            if len(parts) == 1 or not parts[1] or item.isdir():
                continue
            path = parts[1]
            if path.startswith("/") or ".." in Path(path).parts or "\\" in path:
                raise ValueError("unsafe upstream archive path")
            if item.issym() or item.islnk():
                continue  # Never follow/extract links, including upstream binary test fixtures.
            if not item.isfile():
                raise ValueError("unsupported upstream archive member")
            total += item.size
            if total > 64_000_000 or item.size > 4_000_000:
                raise ValueError("upstream archive exceeds preparation limits")
            content = archive.extractfile(item).read()
            try:
                text = content.decode("utf-8")
            except UnicodeDecodeError:
                continue  # Binary assets are outside this text-only pilot.
            if path in files:
                raise ValueError("duplicate archive member")
            files[path] = text
    if len(roots) != 1:
        raise ValueError("upstream archive must have one repository root")
    return files


def download_snapshot(repository: str, commit: str, cache: Path) -> dict[str, str]:
    cache.mkdir(parents=True, exist_ok=True)
    target = cache / f"{repository.replace('/', '--')}-{commit}.tar.gz"
    if not target.exists():
        url = f"https://codeload.github.com/{repository}/tar.gz/{commit}"
        # Preparation is the only networked stage. No .env or model credentials are sent.
        for attempt in range(3):
            try:
                with httpx.stream("GET", url, timeout=60, follow_redirects=True) as response:
                    response.raise_for_status()
                    chunks, size = [], 0
                    for chunk in response.iter_bytes():
                        size += len(chunk)
                        if size > 16_000_000:
                            raise ValueError("upstream download exceeds 16MB")
                        chunks.append(chunk)
                break
            except httpx.TransportError:
                if attempt == 2:
                    raise  # Safe, bounded retries apply only to public archive GETs, never model requests.
        body = b"".join(chunks)
        archive_files(body)  # Validate before retaining a complete cache entry.
        temporary = target.with_suffix(".partial")
        temporary.write_bytes(body)
        temporary.replace(target)
    return archive_files(target.read_bytes())


def build_task(case: RealCase, base: dict[str, str], fixed: dict[str, str], repro: str):
    source_prefix = f"src/{case.package}/"
    public = {}
    for path, text in base.items():
        if any(part.startswith(".") for part in Path(path).parts):
            continue
        if path.startswith(source_prefix) or path.startswith("tests/") or path in {
            "pyproject.toml", "setup.cfg", "README.md", "README.rst", "LICENSE", "LICENSE.txt",
            "LICENSE.APACHE", "LICENSE.BSD", "conftest.py",
        }:
            public[safe_path(path)] = text
    editable = sorted(p for p in public if p.startswith(source_prefix) and p.endswith(".py"))
    if not editable or any(p not in fixed for p in editable):
        raise ValueError("reference must preserve the editable source set")
    for path in [*case.public_tests, *case.regression_tests]:
        safe_path(path)
        if path not in public or path not in fixed or not path.startswith("tests/"):
            raise ValueError("selected upstream test is absent")
    # No reference source, fix diff, PR body or changelog enters solver-visible files.
    public["repro_issue.py"] = repro
    judge = {"upstream_regression/" + p.removeprefix("tests/"): text
             for p, text in public.items() if p.startswith("tests/") and
             (Path(p).name in {"conftest.py", "__init__.py"} or p in case.regression_tests)}
    for path in case.regression_tests:
        judge["upstream_regression/" + path.removeprefix("tests/")] = fixed[path]
    command = ["env", "PYTHONPATH=/workspace/src", "PYTEST_DISABLE_PLUGIN_AUTOLOAD=1", "python", "-m", "pytest",
               "-c", "/dev/null", "-p", "no:cacheprovider", "--basetemp=/tmp/evocoder-tests", "-q"]
    grading = ["upstream_regression/" + p.removeprefix("tests/") for p in case.regression_tests]
    task = Task(id=case.id, source_group=f"{case.repository}#pr-{case.pull_request}",
                repository=case.repository, split=case.split, issue=case.issue,
                files=public, editable=editable, public_command=[*command, *case.public_tests, "repro_issue.py"],
                grader_files=judge, grader_command=[*command, *grading, "-k", case.regression_filter])
    reference = {path: fixed[path] for path in editable}
    provenance = {"case": case.model_dump(), "task_hash": digest(task.model_dump()),
                  "base_text_tree_hash": digest(base), "fixed_text_tree_hash": digest(fixed),
                  "included_base_files": {p: digest(text) for p, text in public.items() if p != "repro_issue.py"},
                  "reference_hash": digest(reference), "reproducer_hash": digest(repro),
                  "snapshot_scope": "regular UTF-8 package source/tests and selected project/license files; links/binaries excluded",
                  "not_official_swe_bench": True, "human_selected_and_adapted": True,
                  "pull_request_url": f"https://github.com/{case.repository}/pull/{case.pull_request}"}
    return task, reference, provenance


def prepare(catalog: Path, output: Path, cache: Path) -> dict:
    if output.exists():
        raise ValueError("real-world bundle destination already exists; frozen assets are not overwritten")
    config = json.loads(catalog.read_text())
    cases = [RealCase.model_validate(row) for row in config["cases"]]
    groups, repositories = {}, {}
    for case in cases:
        group = (case.repository, case.pull_request)
        if group in groups:
            raise ValueError("duplicate upstream PR")
        groups[group] = case.split
        if case.repository in repositories and repositories[case.repository] != case.split:
            raise ValueError("this pilot requires repository-disjoint splits")
        repositories[case.repository] = case.split
    built = []
    for case in cases:
        base = download_snapshot(case.repository, case.base_commit, cache)
        fixed = download_snapshot(case.repository, case.fixed_commit, cache)
        repro = (catalog.parent / safe_path(case.reproducer)).read_text()
        built.append(build_task(case, base, fixed, repro))
    output.mkdir(parents=True)
    entries = []
    for task, reference, provenance in built:
        (output / "tasks").mkdir(exist_ok=True)
        (output / "controller").mkdir(exist_ok=True)
        (output / "tasks" / f"{task.id}.json").write_text(canonical(task.model_dump()) + "\n")
        (output / "controller" / f"{task.id}.json").write_text(canonical({
            "reference": reference, "provenance": provenance}) + "\n")
        entries.append({"path": f"tasks/{task.id}.json", "hash": digest(task.model_dump()),
                        "id": task.id, "split": task.split, "repository": task.repository})
    manifest = {"schema": 1, "kind": "upstream_pr_reconstruction_pilot", "catalog_hash": digest(config),
                "repository_disjoint": True, "tasks": entries,
                "claim": "hand-selected historical defects; not an official benchmark or contamination-free test"}
    (output / "manifest.json").write_text(canonical(manifest) + "\n")
    return manifest


def verify(manifest: Path, settings: Settings, output: Path) -> dict:
    if output.exists():
        raise ValueError("verification receipt already exists")
    rows = []
    for task in load_catalog(manifest):
        controller = json.loads((manifest.parent / "controller" / f"{task.id}.json").read_text())
        reference, provenance = controller["reference"], controller["provenance"]
        if digest(task.model_dump()) != provenance["task_hash"] or digest(reference) != provenance["reference_hash"]:
            raise ValueError("task or controller reference hash mismatch")
        runs = {}
        for name, files, command in [
            ("buggy_public", task.files, task.public_command),
            ("buggy_regression", {**task.files, **task.grader_files}, task.grader_command),
            ("reference_public", {**task.files, **reference}, task.public_command),
            ("reference_regression", {**task.files, **task.grader_files, **reference}, task.grader_command),
        ]:
            runs[name] = validation(files, command, settings, f"{task.id}-{name}")
        # pytest code 1 means actual assertions failed; import/collection/config errors are not a reproduced bug.
        passed = (all(runs[n]["exit_code"] == 1 and not runs[n]["timeout"]
                      for n in ["buggy_public", "buggy_regression"]) and
                  all(runs[n]["exit_code"] == 0 for n in ["reference_public", "reference_regression"]) and
                  len({r["image_id"] for r in runs.values()}) == 1)
        rows.append({"id": task.id, "repository": task.repository, "passed": passed, "checks": runs})
    receipt = {"schema": 1, "passed": all(row["passed"] for row in rows), "rows": rows,
               "manifest_hash": digest(json.loads(manifest.read_text())), "settings": settings.public(),
               "claim": "offline upstream defect/reference verification; zero model calls"}
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n")
    return receipt
