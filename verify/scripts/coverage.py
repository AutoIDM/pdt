"""Check that verify/coverage.yml names every behavior pdt has.

    coverage.py --check

Command names come from the add_parser calls in src/pdt/cli.py and the
config key tables in src/pdt/config.py, read with ast so the check needs
nothing installed beyond pyyaml. Scenario names come from verify.py and
app names from verify/pdt.yml.
"""

from __future__ import annotations

import argparse
import ast
from pathlib import Path

import yaml

PROJECT = Path(__file__).resolve().parent.parent
SRC = PROJECT.parent / "src" / "pdt"
INVENTORY = PROJECT / "coverage.yml"
# Behaviors an app folder turns on that are not config keys.
EXTRA_KEYS = {"Dockerfile"}


def assigned(tree: ast.Module, name: str):
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
                isinstance(target, ast.Name) and target.id == name
                for target in node.targets):
            return ast.literal_eval(node.value)
    raise LookupError(f"{name} is not assigned at module level")


def cli_commands() -> set[str]:
    tree = ast.parse((SRC / "cli.py").read_text())
    names = set(assigned(tree, "CLOUD_CLIS"))
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "add_parser" and node.args
                and isinstance(node.args[0], ast.Constant)):
            names.add(node.args[0].value)
    return names


def config_keys() -> tuple[set[str], set[str]]:
    tree = ast.parse((SRC / "config.py").read_text())
    keys = set(assigned(tree, "APP_KEYS"))
    keys |= {f"platform.{key}" for key in assigned(tree, "PLATFORM_KEYS")}
    keys |= {f"env.{key}" for key in assigned(tree, "ENV_KEYS")}
    return keys, set(assigned(tree, "PROVIDERS"))


def scenario_names() -> set[str]:
    import verify
    return set(verify.SCENARIOS)


def app_names() -> set[str]:
    matrix = yaml.safe_load((PROJECT / "pdt.yml").read_text())
    return {entry["name"] for entry in matrix["apps"]}


def check(data: dict, commands: set[str], keys: set[str], providers: set[str],
          targets: set[str]) -> list[str]:
    problems = []
    for section, wanted in (("commands", commands), ("keys", keys),
                            ("providers", providers)):
        rows = data.get(section) or {}
        for name in sorted(wanted - rows.keys()):
            problems.append(f"{section}: {name} has no row in coverage.yml")
        for name in sorted(rows.keys() - wanted):
            if not (section == "keys" and name.split(".")[0] in wanted):
                problems.append(f"{section}: {name} is in coverage.yml but not in the code")
    for section, rows in data.items():
        for name, row in (rows or {}).items():
            row = row or {}
            if ("covered_by" in row) == ("uncovered" in row):
                problems.append(f"{section}: {name} needs covered_by or uncovered, not both or neither")
            for target in row.get("covered_by") or []:
                if target not in targets:
                    problems.append(f"{section}: {name} names {target}, which is neither a scenario nor an app")
    return problems


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="coverage.py", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--check", action="store_true", required=True)
    parser.parse_args(argv)
    keys, providers = config_keys()
    problems = check(yaml.safe_load(INVENTORY.read_text()), cli_commands(), keys | EXTRA_KEYS,
                     providers, scenario_names() | app_names())
    for problem in problems:
        print(problem)
    print(f"coverage: {len(problems)} problem(s)")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
