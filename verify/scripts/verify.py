"""Deploy every app of one provider, check the account, then destroy them.

    verify.py <provider> [--report FILE]

The scenarios in SCENARIOS run in order and the run stops at the first
failed step. On a cloud provider they assert the account is empty, deploy
every app in verify/pdt.yml order, wait for the a app's schedule to fire,
deploy the a app again, read the data store, then destroy the apps one at a
time and check after each one that the destroyed app is gone and that
nothing else moved. The `local` provider runs only the commands that need
no account. If the initial check fails, the run exits without changing
resources. After that check passes, a failure attempts to destroy every app
and exits 1.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime
import functools
import subprocess
import tempfile
import time
import xml.etree.ElementTree as ET
import zoneinfo
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from inventory import (INVENTORIES, RUNS, SETTINGS, SHARED, UNTAGGED,
                       aws, az, azure_store_names, classify, resource_prefix, store_name)

PROJECT = Path(__file__).resolve().parent.parent
DEADLINE_SECONDS = 180
POLL_SECONDS = 10
FIRE_LEAD_MINUTES = 15
FIRE_GRACE_SECONDS = 8 * 60
FIRE_POLL_SECONDS = 20


@dataclass
class Step:
    name: str
    ok: bool
    detail: str = ""


@dataclass
class Run:
    apps: list[str]
    run_pdt: object
    report: object
    inventory: object = None
    raw_inventory: object = None
    settings: dict = field(default_factory=dict)
    wait: object = None
    runs: object = None
    fire: datetime.datetime | None = None
    timezone: str = "Etc/UTC"
    schedule: object = None
    steps: list[Step] = field(default_factory=list)
    owned: dict = field(default_factory=dict)
    shared: set = field(default_factory=set)

    def record(self, name, problems):
        return record(self.steps, self.report, name, problems)

    def check(self, name, checker, deadline=DEADLINE_SECONDS, poll=POLL_SECONDS):
        return self.record(name, self.wait(self.inventory, checker, deadline, poll))

    def command(self, name, *args, **kwargs):
        code = self.run_pdt(*args, **kwargs)
        return self.record(name, [] if code == 0 else [f"pdt {' '.join(args)} exited {code}"])


def wait_for(inventory, check, deadline=DEADLINE_SECONDS, poll=POLL_SECONDS,
             sleep=time.sleep, clock=time.monotonic):
    """Re-read the account until its listing catches up with the deletions."""
    stop = clock() + deadline
    while True:
        problems = check(inventory())
        if not problems or clock() >= stop:
            return problems
        sleep(poll)


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


def same_check(before):
    def check(resources):
        present = {resource.id for resource in resources}
        return ([f"{item} appeared" for item in sorted(present - before)]
                + [f"{item} disappeared" for item in sorted(before - present)])
    return check


def fired_check(app):
    def check(found):
        return [] if found else [f"no successful run of {app} since its schedule was set"]
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


def fire_schedule(now, lead_minutes=FIRE_LEAD_MINUTES):
    """A daily cron for the next whole minute at least lead_minutes after now."""
    fire = (now + datetime.timedelta(minutes=lead_minutes, seconds=59)).replace(
        second=0, microsecond=0)
    return f"{fire.minute} {fire.hour} * * *", fire


def app_now(timezone):
    if timezone == "local":
        return datetime.datetime.now().astimezone()
    return datetime.datetime.now(zoneinfo.ZoneInfo(timezone))


@contextlib.contextmanager
def scheduled(app_dir, cron):
    path = app_dir / "config.yml"
    original = path.read_text()
    path.write_text(f'{original}schedule: "{cron}"\n')
    try:
        yield
    finally:
        path.write_text(original)


def print_step(step):
    print(f"{'PASS' if step.ok else 'FAIL'}  {step.name}", flush=True)
    for line in step.detail.splitlines():
        print(f"      {line}", flush=True)


def record(steps, report, name, problems):
    step = Step(name, not problems, "\n".join(problems))
    steps.append(step)
    report(step)
    return step.ok


def deploy(ctx):
    for app in ctx.apps:
        if not ctx.command(f"deploy {app}", "deploy", app, "--yes"):
            return False
    if not ctx.record("every resource is tagged",
                      ctx.wait(ctx.raw_inventory, untagged_check(ctx.apps))):
        return False
    ctx.owned, ctx.shared = ownership(ctx.inventory(), ctx.apps)
    return ctx.record("every resource has an owner", owner_problems(ctx.owned, ctx.apps))


def schedule_fires(ctx):
    app = ctx.apps[0]
    if not ctx.command(f"schedule {app} for verification", "deploy", app, "--yes"):
        return False
    since = ctx.fire - datetime.timedelta(minutes=FIRE_LEAD_MINUTES)
    remaining = (ctx.fire - datetime.datetime.now(datetime.timezone.utc)).total_seconds()
    deadline = max(remaining, 0) + FIRE_GRACE_SECONDS
    found = ctx.wait(functools.partial(ctx.runs, app, since), fired_check(app),
                     deadline, FIRE_POLL_SECONDS)
    return ctx.record(f"{app} ran on schedule", found)


def storage(ctx):
    first, second = ctx.apps[0], ctx.apps[1]
    if not ctx.command(f"storage ls {first}", "storage", first, "ls"):
        return False
    with tempfile.TemporaryDirectory(prefix="pdt-storage-verify-") as folder:
        target = str(Path(folder) / "verify.csv")
        if not ctx.command(f"storage get {first}", "storage", first,
                           "get", "verify.csv", target):
            return False
        downloaded = Path(target).read_text()
        if not ctx.record(f"storage get {first} returned the expected data",
                          [] if downloaded == "name,value\nverification,1\n"
                          else ["downloaded verify.csv has unexpected content"]):
            return False
    if not ctx.command(f"storage query {first}", "storage", first,
                       "query", "SELECT count(*) AS rows FROM 'verify.csv'"):
        return False
    if not ctx.command(f"storage destroy {first}", "storage", first, "destroy", "--yes"):
        return False
    code = ctx.run_pdt("storage", second, "ls")
    return ctx.record(f"storage ls {second} is refused",
                      [] if code != 0 else [f"pdt storage {second} ls exited 0 with storage off"])


def storage_cleanup(ctx, required=True):
    if resource_prefix() == "pdt":
        return True
    settings = ctx.settings
    resources = ctx.raw_inventory()
    if settings["provider"] == "aws":
        identity = aws(settings["region"], "sts", "get-caller-identity") or {}
        expected = {store_name(str(identity.get("Account") or ""))}
        command = ("aws", "s3api", "delete-bucket", "--bucket")
    elif settings["provider"] == "google-cloud":
        expected = {store_name(settings["project"])}
        command = ("gcloud", "storage", "buckets", "delete", "--quiet")
    elif settings["provider"] == "azure":
        group, account, _container = azure_store_names(settings["subscription"])
        expected = {group, account}
        command = ("az", "group", "delete", "--name", group, "--yes")
    else:
        return True
    stores = [resource for resource in resources if resource.name in expected]
    problems = []
    missing = expected - {resource.name for resource in stores}
    if required:
        problems.extend(f"expected run store {name} was not found" for name in sorted(missing))
    for resource in stores:
        if resource.tags.get("managed-by") != "pdt":
            problems.append(f"{resource.kind} {resource.name} is not managed by PDT")
            continue
        if resource.tags.get("pdt-lifecycle") != "retain":
            problems.append(f"{resource.kind} {resource.name} has unexpected lifecycle tags")
            continue
        if settings["provider"] == "azure":
            continue
        if settings["provider"] == "google-cloud":
            args = (*command, f"gs://{resource.name}")
        else:
            args = (*command, resource.name)
        if ctx.run_pdt(*args) != 0:
            problems.append(f"could not delete {resource.kind} {resource.name}")
    if not problems and settings["provider"] == "azure":
        if ctx.run_pdt(*command) != 0:
            problems.append(f"could not delete storage resource group {command[4]}")
    if problems:
        return ctx.record("delete run-owned storage", problems)
    remaining = ctx.wait(ctx.raw_inventory,
                         lambda current: [f"{resource.kind} {resource.name} remains"
                                           for resource in current
                                           if resource.name in expected])
    if remaining:
        return ctx.record("delete run-owned storage", remaining)
    return ctx.record("no run-owned resources remain", empty_check(ctx.raw_inventory()))


def redeploy(ctx):
    app = ctx.apps[0]
    before = {resource.id for resource in ctx.inventory()}
    if not ctx.command(f"deploy {app} again", "deploy", app, "--yes"):
        return False
    return ctx.check(f"nothing changed after redeploy {app}", same_check(before))


def destroy(ctx):
    for index, app in enumerate(ctx.apps):
        if not ctx.command(f"destroy {app}", "destroy", app, "--yes"):
            return False
        if not ctx.check(f"{app} resources are gone", gone_check(ctx.owned[app])):
            return False
        if not ctx.check(f"other apps are untouched after destroy {app}",
                         untouched_check(ctx.owned, ctx.shared, ctx.apps[index + 1:])):
            return False
    return ctx.check("account is empty after destroy", empty_check)


def destroy_absent(ctx):
    app = ctx.apps[0]
    if not ctx.command(f"destroy {app} again", "destroy", app, "--yes"):
        return False
    return ctx.check("account is still empty", empty_check)


def local(ctx):
    for args in (("validate",), ("list",), ("examples",),
                 ("completion", "--script", "bash"), ("run", "windows-a")):
        if not ctx.command(" ".join(args), *args):
            return False
    with tempfile.TemporaryDirectory(prefix="pdt-verify-") as folder:
        for args in (("init", "--yes"), ("new", "report", "--from", "hello-world"), ("list",)):
            if not ctx.command(f"{' '.join(args)} in a new folder", *args, cwd=folder):
                return False
        code = ctx.run_pdt("validate", cwd=folder)
        return ctx.record("validate in a new folder reports the missing provider",
                          [] if code != 0 else ["pdt validate exited 0 with no provider set"])


SCENARIOS = {
    "deploy": deploy,
    "schedule-fires": schedule_fires,
    "redeploy": redeploy,
    "storage": storage,
    "destroy": destroy,
    "destroy-absent": destroy_absent,
    "storage-cleanup": storage_cleanup,
    "local": local,
}
def verify(apps, run_pdt, inventory, runs, fire, report=print_step, wait=wait_for,
           raw_inventory=None, settings=None, schedule=contextlib.nullcontext,
           timezone="Etc/UTC"):
    ctx = Run(apps, run_pdt, report, inventory, raw_inventory=raw_inventory or inventory,
              settings=settings or {}, wait=wait, runs=runs, fire=fire, timezone=timezone,
              schedule=schedule)
    cleanup = False
    try:
        try:
            problems = wait(ctx.raw_inventory, empty_check)
            if not ctx.record("account is empty before deploy", problems):
                return ctx.steps
            cleanup = True
            if not SCENARIOS["deploy"](ctx):
                return ctx.steps
            if ctx.fire is None:
                cron, ctx.fire = fire_schedule(app_now(ctx.timezone))
            else:
                cron = f"{ctx.fire.minute} {ctx.fire.hour} * * *"
            with ctx.schedule(cron):
                for name in ("schedule-fires", "redeploy"):
                    if not SCENARIOS[name](ctx):
                        break
            if all(step.ok for step in ctx.steps):
                for name in ("storage", "destroy", "destroy-absent", "storage-cleanup"):
                    if not SCENARIOS[name](ctx):
                        break
        except Exception as exc:
            ctx.record("unexpected error", [f"{type(exc).__name__}: {exc}"])
    finally:
        if cleanup and any(not step.ok for step in ctx.steps):
            cleanup_problems = []
            code = run_pdt("storage", apps[0], "destroy", "--yes")
            if code != 0:
                cleanup_problems.append(
                    f"pdt storage {apps[0]} destroy --yes exited {code}")
            for app in apps:
                code = run_pdt("destroy", app, "--yes")
                if code != 0:
                    cleanup_problems.append(f"pdt destroy {app} --yes exited {code}")
            if cleanup_problems:
                ctx.record("cleanup commands", cleanup_problems)
            try:
                storage_cleanup(ctx, required=False)
            except Exception as exc:
                ctx.record("cleanup after failure", [f"{type(exc).__name__}: {exc}"])
    return ctx.steps


def verify_local(run_pdt, report=print_step):
    ctx = Run([], run_pdt, report)
    local(ctx)
    return ctx.steps


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


def run_pdt(*args: str, cwd=PROJECT) -> int:
    return subprocess.run(["pdt", *args], cwd=cwd, check=False).returncode


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="verify.py", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("provider", choices=sorted(INVENTORIES) + ["local"])
    parser.add_argument("--report", help="write a JUnit XML report to this file")
    args = parser.parse_args(argv)
    if args.provider == "local":
        steps = verify_local(run_pdt)
    else:
        rows = matrix_rows(args.provider)
        if len(rows) < 2:
            print(f"error: verify/pdt.yml needs two apps on provider {args.provider}")
            return 1
        settings = SETTINGS[args.provider](rows[0].get("platform") or {})
        if args.provider == "azure" and not settings["subscription"]:
            settings["subscription"] = str((az("account", "show") or {}).get("id") or "")
        raw_inventory = functools.partial(INVENTORIES[args.provider], settings)
        inventory = functools.partial(listing, raw_inventory)
        runs = functools.partial(RUNS[args.provider], settings)
        schedule = functools.partial(scheduled, PROJECT / rows[0]["name"])
        steps = verify([row["name"] for row in rows], run_pdt, inventory, runs, None,
                       raw_inventory=raw_inventory,
                       settings={**settings, "provider": args.provider}, schedule=schedule,
                       timezone=rows[0].get("timezone", "Etc/UTC"))
    if args.report:
        write_report(args.report, args.provider, steps)
    return 0 if all(step.ok for step in steps) else 1


if __name__ == "__main__":
    raise SystemExit(main())
