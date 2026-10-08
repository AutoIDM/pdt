"""Find the env vars a Python app's code reads, with Python's ast module.

pdt parses `run.py` and the other `.py` files in the app folder and runs none
of them. `os.environ["X"]` is a required read; `os.environ.get("X")` and
`os.getenv("X")`, with or without a default, are optional reads. The forms
also count through `import os as o` and `from os import environ, getenv`.
A name the code builds at run time is not found.
"""

from __future__ import annotations

import ast
from pathlib import Path

from pdt.powershell import SYSTEM_ENV_VARS


def app_files(folder: Path) -> list[Path]:
    """run.py first, then the other .py files in the app folder in name order."""
    files = sorted(path for path in Path(folder).glob("*.py") if path.is_file())
    return sorted(files, key=lambda path: path.name != "run.py")


def reads(folder: Path) -> list[tuple[str, int, str]]:
    """(file:line, name, kind) of each env var read with a literal name, in file and line
    order. kind is "required", "optional", or "set" (an assignment, setdefault, putenv, or
    del). A file that does not parse gives none."""
    found = []
    for path in app_files(folder):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (SyntaxError, UnicodeDecodeError, ValueError):
            continue
        modules = {"os"}
        environs: set[str] = set()
        getenvs: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules |= {alias.asname or alias.name for alias in node.names if alias.name == "os"}
            elif isinstance(node, ast.ImportFrom) and node.module == "os":
                for alias in node.names:
                    if alias.name == "environ":
                        environs.add(alias.asname or alias.name)
                    elif alias.name == "getenv":
                        getenvs.add(alias.asname or alias.name)

        def is_os(node) -> bool:
            return isinstance(node, ast.Name) and node.id in modules

        def is_environ(node) -> bool:
            if isinstance(node, ast.Name):
                return node.id in environs
            return isinstance(node, ast.Attribute) and node.attr == "environ" and is_os(node.value)

        def literal(node) -> str | None:
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                return node.value
            return None

        file_reads = []
        for node in ast.walk(tree):
            name, kind = None, None
            if isinstance(node, ast.Subscript) and is_environ(node.value):
                name = literal(node.slice)
                kind = "required" if isinstance(node.ctx, ast.Load) else "set"
            elif isinstance(node, ast.Call) and node.args:
                func = node.func
                name = literal(node.args[0])
                if isinstance(func, ast.Attribute) and is_environ(func.value):
                    kind = {"get": "optional", "setdefault": "set"}.get(func.attr)
                elif isinstance(func, ast.Attribute) and is_os(func.value):
                    kind = {"getenv": "optional", "putenv": "set"}.get(func.attr)
                elif isinstance(func, ast.Name) and func.id in getenvs:
                    kind = "optional"
            if name is not None and kind is not None:
                file_reads.append((node.lineno, name, kind))
        found.extend((f"{path.name}:{line}", name, kind) for line, name, kind in sorted(file_reads))
    return found


def unlisted_env_reads(folder: Path, env: dict) -> tuple[dict[str, str], list[str]]:
    """({name: "file:line"} of each required read, [name] of each optional read) for the
    names the code does not set, the env spec does not list, and the system or pdt does
    not set. A name with a required read anywhere is required."""
    listed = {name.lower() for key in ("required", "optional") for name in env.get(key) or []}
    listed |= {name.lower() for group in env.get("one_of") or [] for name in group}
    found = reads(folder)
    listed |= {name.lower() for _where, name, kind in found if kind == "set"}
    required: dict[str, str] = {}
    optional: dict[str, str] = {}
    for kind in ("required", "optional"):
        for where, name, read_kind in found:
            key = name.lower()
            if read_kind != kind or key in listed or key in SYSTEM_ENV_VARS or key.startswith("pdt_"):
                continue
            listed.add(key)
            (required if kind == "required" else optional)[name] = where
    return required, list(optional)
