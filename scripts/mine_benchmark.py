"""Offline candidate mining. Historical commit messages define prompts; not official SWE-bench."""

import ast
import io
import json
import re
import subprocess
import tarfile
from pathlib import Path

from evocoder.contracts import Task, canonical, digest

ROOT = Path("work/benchmark-repos")
OUT = Path("work/benchmark-candidates")


def git(repo, *args, binary=False):
    r = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, timeout=60, check=True)
    return r.stdout if binary else r.stdout.decode()


def test_path(path):
    return any(p in {"tests", "test"} for p in path.split("/")) or Path(path).name.startswith("test_")


def tree(repo, commit):
    result = {}
    with tarfile.open(
        fileobj=io.BytesIO(git(repo, "archive", "--format=tar", commit, binary=True))
    ) as archive:
        for item in archive:
            if item.isfile() and item.size < 500000:
                try:
                    result[item.name] = archive.extractfile(item).read().decode("utf-8")
                except UnicodeDecodeError:
                    pass
    return result


def build(repo, repository, commit):
    parent = git(repo, "rev-parse", commit + "^").strip()
    message = git(repo, "show", "-s", "--format=%B", commit).strip()
    changed = git(repo, "diff", "--name-only", parent, commit).splitlines()
    sources = [
        p
        for p in changed
        if p.endswith(".py")
        and not test_path(p)
        and "/" in p
        and p.split("/")[0] not in {"docs", "examples", "benchmarks", "tools", "scripts", "changelog.d"}
    ]
    tests = [p for p in changed if p.endswith(".py") and test_path(p) and Path(p).name != "conftest.py"]
    if (
        not 1 <= len(sources) <= 3
        or not 1 <= len(tests) <= 2
        or any(p.endswith("conftest.py") for p in changed)
    ):
        raise ValueError("not a bounded source-and-regression-test change")
    base, fixed = tree(repo, parent), tree(repo, commit)
    if any(p not in base or p not in fixed for p in sources + tests):
        raise ValueError("added/removed source or test file outside this adapter")
    prefixes = {p.split("/")[0] + "/" for p in sources}
    files = {
        p: t
        for p, t in base.items()
        if any(p.startswith(pre) for pre in prefixes)
        and not test_path(p)
        and p.endswith(".py")
        and not any(x.startswith(".") for x in Path(p).parts)
    }
    editable = sorted(files)
    if not files or any(p not in fixed for p in editable):
        raise ValueError("source tree cannot be reconstructed")
    # Public tests are the old tests, hidden regression tests are the fixed upstream tests.
    test_roots = {p.split("/")[0] + "/" for p in tests}
    base_tests = {
        p: t
        for p, t in base.items()
        if any(p.startswith(pre) for pre in test_roots) and not any(x.startswith(".") for x in Path(p).parts)
    }
    fixed_tests = {
        p: t
        for p, t in fixed.items()
        if any(p.startswith(pre) for pre in test_roots) and not any(x.startswith(".") for x in Path(p).parts)
    }

    def public_name(p):
        return "public_checks/" + p.split("/", 1)[1]

    files.update({public_name(p): t for p, t in base_tests.items()})
    if "conftest.py" in base:
        files["conftest.py"] = base["conftest.py"]
    for license_path in ["LICENSE", "LICENSE.txt", "LICENSE.rst", "COPYING"]:
        if license_path in base:
            files[license_path] = base[license_path]
    cmd = [
        "env",
        "PYTHONPATH=/workspace/src:/workspace",
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD=1",
        "python",
        "-m",
        "pytest",
        "-c",
        "/dev/null",
        "-p",
        "no:cacheprovider",
        "--basetemp=/tmp/benchmark-tests",
        "-q",
    ]
    ident = repository.replace("/", "-") + "-" + commit[:10]
    patch = git(repo, "diff", "--unified=0", parent, commit, "--", *sources)
    fingerprint = digest(
        [
            line.strip()
            for line in patch.splitlines()
            if line.startswith(("+", "-")) and not line.startswith(("+++", "---"))
        ]
    )
    functions = []
    for path in sources:
        source_diff = git(repo, "diff", "--unified=0", parent, commit, "--", path)
        points = [int(m.group(1)) for m in re.finditer(r"^@@ .*\+(\d+)", source_diff, re.M)]
        nodes = [
            n
            for n in ast.walk(ast.parse(fixed[path]))
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        ]
        for point in points:
            scope = [n for n in nodes if n.lineno <= point <= n.end_lineno]
            function = min(scope, key=lambda n: n.end_lineno - n.lineno).name if scope else "<module>"
            functions.append(path + "::" + function)
    group = repository + "::" + ",".join(sorted(set(functions)))
    task = Task(
        id=ident,
        source_group=group,
        repository=repository,
        split="learn",
        issue=(
            "Repair the historical defect described by the upstream developer below. Preserve existing "
            "behavior outside this defect and verify your change using the available old tests. "
            "Additional upstream regression tests are held back.\n\n" + message
        ),
        files=files,
        editable=editable,
        public_command=[*cmd, *[public_name(t) for t in tests]],
        grader_files=fixed_tests,
        grader_command=[*cmd, *tests],
    )
    return task, {
        "reference": {p: fixed[p] for p in editable},
        "repository": repository,
        "base_commit": parent,
        "fixed_commit": commit,
        "commit_url": f"https://github.com/{repository}/commit/{commit}",
        "message": message,
        "patch_fingerprint": fingerprint,
        "functions": sorted(set(functions)),
        "claim": "commit-message-defined historical defect; selected tests and text source; no contamination-free claim",
    }


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    selected, rejected, groups, fingerprints = [], [], set(), set()
    for repo in sorted(ROOT.iterdir()):
        if not (repo / ".git").is_dir():
            continue
        repository = repo.name.replace("--", "/")
        hashes = git(
            repo, "log", "--format=%H", "--no-merges", "--regexp-ignore-case", "--grep=fix"
        ).splitlines()
        for commit in hashes:
            try:
                task, controller = build(repo, repository, commit)
                if task.source_group in groups or controller["patch_fingerprint"] in fingerprints:
                    raise ValueError("duplicate function group or exact patch fingerprint")
                groups.add(task.source_group)
                fingerprints.add(controller["patch_fingerprint"])
                folder = OUT / task.id
                folder.mkdir(exist_ok=True)
                (folder / "task.json").write_text(canonical(task.model_dump()) + "\n")
                controller["task_hash"] = digest(task.model_dump())
                (folder / "controller.json").write_text(canonical(controller) + "\n")
                selected.append({"id": task.id, "repository": repository, "group": task.source_group})
                print(json.dumps({"candidate": task.id, "total": len(selected)}), flush=True)
            except Exception as exc:
                rejected.append({"repo": repository, "commit": commit, "reason": str(exc)[:300]})
    (OUT / "index.json").write_text(
        json.dumps({"candidates": selected, "rejected": rejected}, indent=2) + "\n"
    )
    print({"candidates": len(selected), "rejected": len(rejected)}, flush=True)


if __name__ == "__main__":
    main()
