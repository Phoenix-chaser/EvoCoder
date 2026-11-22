"""A bounded, non-executing view of the public test interface for semantic routing."""

from __future__ import annotations

import ast
import re
from itertools import islice
from pathlib import PurePosixPath

from evocoder.contracts import Task


def identifier(name: str) -> str:
    return name if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}", name) else "expression"


def symbol(node: ast.AST) -> str:
    name = node.id if isinstance(node, ast.Name) else node.attr if isinstance(node, ast.Attribute) else ""
    return identifier(name)


def form(node: ast.AST, depth: int = 0) -> str:
    if isinstance(node, ast.Constant):
        return "null" if node.value is None else type(node.value).__name__
    if isinstance(node, ast.Call):
        return "call:" + symbol(node.func)
    if isinstance(node, ast.Name):
        return "name:" + symbol(node)
    if isinstance(node, (ast.Dict, ast.List, ast.Tuple, ast.Set)) and depth < 1:
        values = node.values if isinstance(node, ast.Dict) else node.elts
        kinds = sorted({form(item, depth + 1) for item in values[:4]})
        return type(node).__name__.lower() + "[" + ",".join(kinds) + "]"
    return type(node).__name__.lower()


def public_test_paths(task: Task) -> list[str]:
    argv = task.public_command
    if not argv:
        return []
    executable = PurePosixPath(argv[0]).name
    args = argv[1:]
    python = re.fullmatch(r"python(?:3(?:\.\d+)?)?", executable)
    if python and "-m" in args:
        args = args[args.index("-m") + 1 :]
        executable, args = (args[0], args[1:]) if args else ("", [])
        python = False
    if not python and executable not in {"pytest", "py.test"}:
        return []
    available = sorted(k for k in task.files if k.endswith(".py") and k not in task.editable)
    if python:
        # python -c/- and unsupported wrappers do not authorize scanning arbitrary files.
        if "-c" in args or "-" in args:
            return []
        script = next((a.removeprefix("./") for a in args if not a.startswith("-")), None)
        return [script] if script in available else []
    selected = []
    # A specific path or node selector limits discovery. Option values are never source text.
    paths = [a.split("::", 1)[0].removeprefix("./").rstrip("/") for a in args if not a.startswith("-")]
    scoped = [p for p in paths if p.endswith(".py") or any(f.startswith(p + "/") for f in available)]
    for path in available:
        name = PurePosixPath(path).name
        test_name = name.startswith("test_") or name.endswith("_test.py")
        if path in scoped or (test_name and (not scoped or any(path.startswith(p + "/") for p in scoped))):
            selected.append(path)
    return selected[:3]


def public_test_shape(task: Task) -> list[str]:
    signals = []
    remaining = 3800

    def add(value):
        nonlocal remaining
        size = len(value.encode()) + 4
        if size <= min(204, remaining) and value not in signals and len(signals) < 56:
            signals.append(value)
            remaining -= size

    def filename(path):
        name = PurePosixPath(path).name
        return name if re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", name) else "file"

    for path in sorted(task.editable)[:4]:
        add("editable file: " + filename(path))
    for path in public_test_paths(task):
        add("public test file: " + filename(path))
        source = task.files[path]
        if len(source.encode()) > 65536:
            continue
        try:
            tree = ast.parse(source)
        except (SyntaxError, ValueError, RecursionError):
            continue
        nodes = list(islice(ast.walk(tree), 1200))
        # Prioritize the behavior checked by assertions over helpers that may only be declared.
        for n in nodes:
            if isinstance(n, ast.Assert) and isinstance(n.test, ast.Compare):
                left = n.test.left
                for op, right in zip(n.test.ops, n.test.comparators):
                    add(f"assert {type(op).__name__}: {form(left)} / {form(right)}")
                    left = right
            elif isinstance(n, ast.ExceptHandler) and n.type is not None:
                add("catches: " + symbol(n.type))
        for n in nodes:
            if isinstance(n, ast.ClassDef):
                add(
                    "fixture class: "
                    + identifier(n.name)
                    + "("
                    + ",".join(symbol(b) for b in n.bases[:3])
                    + ")"
                )
                for member in n.body:
                    if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        add("declared method: " + identifier(member.name))
            elif isinstance(n, ast.Call):
                args = [form(a) for a in n.args[:4]]
                args.extend("keyword:" + (identifier(k.arg) if k.arg else "mapping") for k in n.keywords[:4])
                add(f"call {symbol(n.func)}({', '.join(args)})")
    return signals
