"""Deploy every app of one provider, check the account, then destroy them.

    verify.py <provider> [--report FILE]

The scenario is fixed. It asserts the account is empty, deploys every app
in verify/pdt.yml order, reads each app's run history with `pdt health` and
`pdt runs` (no app has run yet, so this proves the read path), records which resource each app owns and which
resources the apps share, then destroys the apps one at a time and checks
after each one that the destroyed app is gone and that nothing else moved.
If the initial check fails, the run exits without changing resources.
After that check passes, a failure attempts to destroy every app and exits 1.
"""

from __future__ import annotations

import argparse
import functools
import subprocess
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

import yaml

from inventory import INVENTORIES, SETTINGS, SHARED, UNTAGGED, aws, classify

PROJECT = Path(__file__).resolve().parent.parent
DEADLINE_SECONDS = 180
POLL_SECONDS = 10
RETIRED_AWS_APP = "salesforce-netsuite-customer-sync"
RETIRED_AWS_RESOURCES = {
    ("ecr repository", "pdt"),
    ("ecs cluster", "pdt"),
    ("iam role", f"pdt-{RETIRED_AWS_APP}-execution"),
    ("iam role", f"pdt-{RETIRED_AWS_APP}-scheduler"),
    ("iam role", f"pdt-{RETIRED_AWS_APP}-task"),
    ("log group", f"/ecs/pdt-{RETIRED_AWS_APP}"),
    ("schedule group", "pdt"),
    ("schedule", f"pdt-{RETIRED_AWS_APP}"),
    ("secret", f"pdt-{RETIRED_AWS_APP}-env"),
}


@dataclass
class Step:
    name: str
    ok: bool
    detail: str = ""


def wait_for(inventory, check, sleep=time.sleep, clock=time.monotonic):
    """Re-read the account until its listing catches up with the deletions."""
    deadline = clock() + DEADLINE_SECONDS
    while True:
        problems = check(inventory())
        if not problems or clock() >= deadline:
            return problems
        sleep(POLL_SECONDS)


def kept_by_design(resource):
    """A data store outlives its apps, so no check expects it to go."""
    return resource.tags.get("pdt-lifecycle") == "retain"


def listing(inventory):
    return [resource for resource in inventory() if not kept_by_design(resource)]


def empty_check(resources):
    return [f"{resource.kind} {resource.name or resource.id} still exists"
            for resource in resources]


def untagged_check(apps):
    def check(resources):
        return [f"{resource.kind} {resource.name or resource.id} "
                "carries no managed-by=pdt tag"
                for resource in resources if classify(resource, apps) == UNTAGGED]
    return check


def gone_check(ids):
    def check(resources):
        present = {resource.id for resource in resources}
        return [f"{item} still exists" for item in sorted(ids & present)]
    return check


def untouched_check(owned, shared, remaining):
    expected = set(shared) if remaining else set()
    for app in remaining:
        expected |= owned[app]

    def check(resources):
        present = {resource.id for resource in resources}
        return [f"{item} is gone but should still exist"
                for item in sorted(expected - present)]
    return check


def ownership(resources, apps):
    owned = {app: set() for app in apps}
    shared = set()
    for resource in resources:
        owner = classify(resource, apps)
        if owner == SHARED:
            shared.add(resource.id)
        else:
            owned[owner].add(resource.id)
    return owned, shared


def owner_problems(owned, apps):
    return [f"{app} owns no resource" for app in apps if not owned[app]]


def print_step(step):
    print(f"{'PASS' if step.ok else 'FAIL'}  {step.name}", flush=True)
    for line in step.detail.splitlines():
        print(f"      {line}", flush=True)


def record(steps, report, name, problems):
    step = Step(name, not problems, "\n".join(problems))
    steps.append(step)
    report(step)
    return step.ok


def scenario(steps, apps, run_pdt, inventory, report, wait):
    def check(name, checker):
        return record(steps, report, name, wait(inventory, checker))

    def command(*args):
        code = run_pdt(*args)
        name = " ".join(arg for arg in args if arg != "--yes")
        return record(steps, report, name,
                      [] if code == 0 else [f"pdt {name} exited {code}"])

    for app in apps:
        if not command("deploy", app, "--yes"):
            return
    for app in apps:
        if not command("health", app) or not command("runs", app):
            return
    if not check("every resource is tagged", untagged_check(apps)):
        return
    owned, shared = ownership(inventory(), apps)
    if not record(steps, report, "every resource has an owner",
                  owner_problems(owned, apps)):
        return
    for index, app in enumerate(apps):
        if not command("destroy", app, "--yes"):
            return
        if not check(f"{app} resources are gone", gone_check(owned[app])):
            return
        if not check(f"other apps are untouched after destroy {app}",
                     untouched_check(owned, shared, apps[index + 1:])):
            return
    check("account is empty after destroy", empty_check)


def cleanup_retired_aws(resources, settings):
    found = {(resource.kind, resource.name or resource.id) for resource in resources}
    if not found or not found <= RETIRED_AWS_RESOURCES:
        return
    if any(classify(resource, []) == UNTAGGED for resource in resources):
        return

    region = settings["region"]
    base = f"pdt-{RETIRED_AWS_APP}"
    if ("schedule", base) in found:
        aws(region, "scheduler", "delete-schedule", "--name", base, "--group-name", "pdt")
    if ("secret", f"{base}-env") in found:
        aws(region, "secretsmanager", "delete-secret", "--secret-id", f"{base}-env",
            "--force-delete-without-recovery")
    if ("log group", f"/ecs/{base}") in found:
        aws(region, "logs", "delete-log-group", "--log-group-name", f"/ecs/{base}")
    for suffix in ("scheduler", "task", "execution"):
        role = f"{base}-{suffix}"
        if ("iam role", role) not in found:
            continue
        policies = aws(region, "iam", "list-role-policies", "--role-name", role) or {}
        for policy in policies.get("PolicyNames") or []:
            aws(region, "iam", "delete-role-policy", "--role-name", role,
                "--policy-name", policy)
        aws(region, "iam", "delete-role", "--role-name", role)
    if ("schedule group", "pdt") in found:
        aws(region, "scheduler", "delete-schedule-group", "--name", "pdt")
    if ("ecs cluster", "pdt") in found:
        aws(region, "ecs", "delete-cluster", "--cluster", "pdt")
    if ("ecr repository", "pdt") in found:
        aws(region, "ecr", "delete-repository", "--repository-name", "pdt", "--force")


def recover(steps, apps, run_pdt, inventory, report, cleanup_leftovers):
    leftovers = inventory()
    if not leftovers or untagged_check(apps)(leftovers):
        return
    detail = "\n".join(f"{resource.kind} {resource.name or resource.id} "
                       "is left over from an earlier run"
                       for resource in leftovers)
    for app in apps:
        run_pdt("destroy", app, "--yes")
    cleanup_leftovers(inventory())
    step = Step("leftovers from an earlier run are destroyed", True, detail)
    steps.append(step)
    report(step)


def verify(apps, run_pdt, inventory, report=print_step, wait=wait_for,
           cleanup_leftovers=None):
    steps: list[Step] = []
    cleanup = False
    try:
        try:
            if cleanup_leftovers is not None:
                recover(steps, apps, run_pdt, inventory, report, cleanup_leftovers)
            problems = wait(inventory, empty_check)
            if not record(steps, report, "account is empty before deploy", problems):
                return steps
            cleanup = True
            scenario(steps, apps, run_pdt, inventory, report, wait)
        except Exception as exc:
            record(steps, report, "unexpected error",
                   [f"{type(exc).__name__}: {exc}"])
    finally:
        if cleanup and any(not step.ok for step in steps):
            for app in apps:
                run_pdt("destroy", app, "--yes")
    return steps


def junit_tree(provider, steps):
    failures = sum(1 for step in steps if not step.ok)
    suite = ET.Element("testsuite", name=f"verify:{provider}",
                       tests=str(len(steps)), failures=str(failures))
    for step in steps:
        case = ET.SubElement(suite, "testcase", classname=f"verify.{provider}",
                             name=step.name)
        if not step.ok:
            failure = ET.SubElement(
                case, "failure",
                message=step.detail.splitlines()[0] if step.detail else step.name)
            failure.text = step.detail
    return ET.ElementTree(suite)


def write_report(path, provider, steps):
    junit_tree(provider, steps).write(path, encoding="utf-8", xml_declaration=True)


def matrix_rows(provider):
    matrix = yaml.safe_load((PROJECT / "pdt.yml").read_text())
    return [entry for entry in matrix["apps"]
            if (entry.get("platform") or {}).get("provider") == provider]


def run_pdt(*args: str) -> int:
    return subprocess.run(["pdt", *args], cwd=PROJECT, check=False).returncode


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="verify.py", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("provider", choices=sorted(INVENTORIES))
    parser.add_argument("--report", help="write a JUnit XML report to this file")
    args = parser.parse_args(argv)
    rows = matrix_rows(args.provider)
    if not rows:
        print(f"error: no app in verify/pdt.yml uses provider {args.provider}")
        return 1
    settings = SETTINGS[args.provider](rows[0].get("platform") or {})
    inventory = functools.partial(listing, functools.partial(INVENTORIES[args.provider], settings))
    cleanup_leftovers = (functools.partial(cleanup_retired_aws, settings=settings)
                         if args.provider == "aws" else None)
    steps = verify([row["name"] for row in rows], run_pdt, inventory,
                   cleanup_leftovers=cleanup_leftovers)
    if args.report:
        write_report(args.report, args.provider, steps)
    return 0 if all(step.ok for step in steps) else 1


if __name__ == "__main__":
    raise SystemExit(main())
