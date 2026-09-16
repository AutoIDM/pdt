#!/usr/bin/env python3
"""Generate the app directories from the matrix in verify/pdt.yml.

The templates live in scripts/templates/ rather than in this file because uv
scans a script for a PEP 723 block and would find the one inside run.py.txt.
Every app gets run.py. The variant after the last `-` in the app's name
(`a` or `b`) picks its config.yml, and the b app of a container provider
also gets its own Dockerfile.
"""

import argparse
import ast
import functools
from pathlib import Path

import yaml

from coverage import SRC, assigned

ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = Path(__file__).resolve().parent / "templates"
CONTAINER_PROVIDERS = assigned(ast.parse((SRC / "config.py").read_text()), "CONTAINER_PROVIDERS")


@functools.cache
def template(path):
    return path.read_text()


def wanted_files(entry):
    variant = entry["name"].rsplit("-", 1)[-1]
    files = {"run.py": TEMPLATES / "run.py.txt",
             "config.yml": TEMPLATES / variant / "config.yml.txt"}
    dockerfile = TEMPLATES / variant / "Dockerfile.txt"
    if dockerfile.is_file() and entry["platform"]["provider"] in CONTAINER_PROVIDERS:
        files["Dockerfile"] = dockerfile
    return {name: template(path).replace("{app}", entry["name"])
            for name, path in files.items()}


def reconcile(path, wanted, check):
    """Return 1 when the file differs from wanted (None = must not exist); fix it unless check."""
    current = path.read_text() if path.is_file() else None
    if current == wanted:
        return 0
    shown = path.relative_to(ROOT)
    if check:
        print(f"mismatch {shown}")
        return 1
    if wanted is None:
        path.unlink()
        print(f"removed {shown}")
    else:
        path.parent.mkdir(exist_ok=True)
        path.write_text(wanted)
        print(f"wrote {shown}")
    return 0


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()

    matrix = yaml.safe_load((ROOT / "pdt.yml").read_text())
    mismatches = 0
    for entry in matrix["apps"]:
        files = wanted_files(entry)
        files.setdefault("Dockerfile", None)
        for name, wanted in files.items():
            mismatches += reconcile(ROOT / entry["name"] / name, wanted, args.check)
    return 1 if mismatches else 0


if __name__ == "__main__":
    raise SystemExit(main())
