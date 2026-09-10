"""Deploy every app of one provider, check the account, then destroy them.

    verify.py <provider> [--report FILE]

The scenario is fixed. It asserts the account is empty, deploys every app
in verify/pdt.yml order, records which resource each app owns and which
resources the apps share, then destroys the apps one at a time and checks
after each one that the destroyed app is gone and that nothing else moved.
Any failure destroys every app again and exits 1, so a failed run leaves
no resource behind.
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

from inventory import INVENTORIES, SETTINGS, SHARED, UNTAGGED, classify

PROJECT = Path(__file__).resolve().parent.parent
DEADLINE_SECONDS = 180
POLL_SECONDS = 10


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
    print(f"{'PASS' if step.ok else 'FAIL'}  {step.name}")
    for line in step.detail.splitlines():
        print(f"      {line}")


def record(steps, report, name, problems):
    step = Step(name, not problems, "\n".join(problems))
    steps.append(step)
    report(step)
    return step.ok


def scenario(steps, apps, run_pdt, inventory, report, wait):
    def check(name, checker):
        return record(steps, report, name, wait(inventory, checker))

    def command(verb, app):
        code = run_pdt(verb, app, "--yes")
        return record(steps, report, f"{verb} {app}",
                      [] if code == 0 else [f"pdt {verb} {app} exited {code}"])

    if not check("account is empty", empty_check):
        return
    for app in apps:
        if not command("deploy", app):
            return
    if not check("every resource is tagged", untagged_check(apps)):
        return
    owned, shared = ownership(inventory(), apps)
    if not record(steps, report, "every resource has an owner",
                  owner_problems(owned, apps)):
        return
    for index, app in enumerate(apps):
        if not command("destroy", app):
            return
        if not check(f"{app} resources are gone", gone_check(owned[app])):
            return
        if not check("other apps are untouched",
                     untouched_check(owned, shared, apps[index + 1:])):
            return
    check("account is empty", empty_check)


def verify(apps, run_pdt, inventory, report=print_step, wait=wait_for):
    steps: list[Step] = []
    try:
        try:
            scenario(steps, apps, run_pdt, inventory, report, wait)
        except Exception as exc:  # noqa: BLE001 - any failure must still tear down
            record(steps, report, "unexpected error",
                   [f"{type(exc).__name__}: {exc}"])
    finally:
        if any(not step.ok for step in steps):
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
    inventory = functools.partial(INVENTORIES[args.provider], settings)
    steps = verify([row["name"] for row in rows], run_pdt, inventory)
    if args.report:
        write_report(args.report, args.provider, steps)
    return 0 if all(step.ok for step in steps) else 1


if __name__ == "__main__":
    raise SystemExit(main())
