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
REQUIRED_RULES = {
    "runs-every-scenario-on-every-provider",
    "cleans-up-after-the-run",
    "fails-when-a-provider-cannot-deploy-or-manage",
    "isolates-concurrent-ci-runs",
    "every-resource-is-tagged",
    "each-app-destroys-individually",
    "deletes-run-owned-storage",
    "shared-resources-go-with-the-last-app",
    "jobs-fire-on-schedule",
    "deploy-reconciles",
    "plan-and-cost-before-changes",
    "cost-estimates-use-provider-prices",
    "cheapest-option-by-default",
    "no-package-source-in-the-build-context",
    "own-dockerfile-is-used",
    "pinned-version-stays-deployed",
    "per-app-names-and-managed-by-tag",
    "destroy-removes-everything",
    "destroy-of-an-undeployed-app-is-safe",
    "azure-shared-environment",
    "no-auto-created-side-resources",
    "destroy-prints-then-asks",
    "guide-the-user",
    "schedules-translate-to-the-provider",
}


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
    keys |= {f"project.{key}" for key in assigned(tree, "ROOT_KEYS")}
    keys |= {f"platform.{key}" for key in assigned(tree, "PLATFORM_KEYS")}
    keys |= {f"env.{key}" for key in assigned(tree, "ENV_KEYS")}
    keys |= {f"retired.{key}" for key in assigned(tree, "RETIRED_KEYS")}
    return keys, set(assigned(tree, "PROVIDERS"))


def scenario_names() -> set[str]:
    import verify
    return set(verify.SCENARIOS)


def app_names() -> set[str]:
    matrix = yaml.safe_load((PROJECT / "pdt.yml").read_text())
    return {entry["name"] for entry in matrix["apps"]}


def matrix_problems(providers: set[str]) -> list[str]:
    matrix = yaml.safe_load((PROJECT / "pdt.yml").read_text())
    rows = matrix.get("apps") or []
    problems = []
    for provider in sorted(providers):
        apps = [row for row in rows
                if (row.get("platform") or {}).get("provider") == provider]
        variants = {row["name"].rsplit("-", 1)[-1]: row for row in apps}
        if len(apps) != 2 or set(variants) != {"a", "b"}:
            problems.append(f"matrix: {provider} needs exactly one a app and one b app")
            continue
        if variants["a"].get("storage", True) is not True:
            problems.append(f"matrix: {provider} a must enable storage")
        if variants["b"].get("storage", True) is not False:
            problems.append(f"matrix: {provider} b must disable storage")
        a_config = yaml.safe_load((PROJECT / variants["a"]["name"] / "config.yml").read_text())
        b_config = yaml.safe_load((PROJECT / variants["b"]["name"] / "config.yml").read_text())
        if "PDT_VERIFY_OPTIONAL" not in ((a_config.get("env") or {}).get("optional") or []):
            problems.append(f"matrix: {provider} a must cover env.optional")
        if ((b_config.get("env") or {}).get("one_of")
                != [["PDT_VERIFY_ALT"], ["PDT_VERIFY_FALLBACK"]]):
            problems.append(f"matrix: {provider} b must cover the alternate env.one_of branch")
    return problems


def ci_problems() -> list[str]:
    text = (PROJECT / ".gitlab-ci.yml").read_text()
    global_config, verify_config = text.split(".verify:", 1)
    verify_config = verify_config.split(".verify:unix:", 1)[0]
    problems = []
    namespace = "PDT_RESOURCE_NAMESPACE: $CI_PIPELINE_ID"
    if namespace not in verify_config:
        problems.append("ci: provider jobs need a CI_PIPELINE_ID resource namespace")
    if "PDT_RESOURCE_NAMESPACE:" in global_config:
        problems.append("ci: the resource namespace must not affect non-provider jobs")
    if "resource_group:" in text:
        problems.append("ci: provider verification jobs must not use resource groups")
    if "when: manual" in text or "allow_failure: true" in text:
        problems.append("ci: every provider verification job must be required")
    return problems


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
    rules = data.get("rules") or {}
    for name in sorted(REQUIRED_RULES - rules.keys()):
        problems.append(f"rules: {name} has no row in coverage.yml")
    for name in sorted(rules.keys() - REQUIRED_RULES):
        problems.append(f"rules: {name} is not in the required rule set")
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
    problems += matrix_problems(providers)
    problems += ci_problems()
    for problem in problems:
        print(problem)
    print(f"coverage: {len(problems)} problem(s)")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
