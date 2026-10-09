#!/usr/bin/env python3
"""Generate the app directories from the matrix in verify/pdt.yml.

The templates live in scripts/templates/ rather than in this file because uv
scans a script for a PEP 723 block and would find the one inside run.py.txt.

With --wheel, each app folder gets a copy of that wheel, and its run.py
installs pdt-cli from the copy instead of PyPI.
"""

import argparse
import shutil
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = Path(__file__).resolve().parent / "templates"
FILES = ("run.py", "config.yml")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--wheel", type=Path)
    args = parser.parse_args()

    templates = {name: (TEMPLATES / f"{name}.txt").read_text() for name in FILES}
    if args.wheel:
        templates["run.py"] = templates["run.py"].replace(
            "# ///\n",
            f'#\n# [tool.uv.sources]\n# pdt-cli = {{ path = "{args.wheel.name}" }}\n# ///\n', 1)
    matrix = yaml.safe_load((ROOT / "pdt.yml").read_text())
    mismatches = 0
    for entry in matrix["apps"]:
        if args.wheel:
            shutil.copy(args.wheel, ROOT / entry["name"])
        for name in FILES:
            path = ROOT / entry["name"] / name
            wanted = templates[name]
            if path.is_file() and path.read_text() == wanted:
                continue
            if args.check:
                print(f"mismatch {path.relative_to(ROOT)}")
                mismatches += 1
            else:
                path.parent.mkdir(exist_ok=True)
                path.write_text(wanted)
                print(f"wrote {path.relative_to(ROOT)}")
    return 1 if mismatches else 0


if __name__ == "__main__":
    raise SystemExit(main())
