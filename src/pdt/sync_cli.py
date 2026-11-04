"""`pdt sync up|down [APP]`: send this project's copy to AutoIDM, or write AutoIDM's copy here.

A copy is the current zip of each unit of the project (pdt.copies): `root`, and `app=<name>` for
each app folder. `pdt sync up` sends each unit, or one app's unit. `pdt sync down` writes each
unit, or one app's unit, into this project: it adds each new file, replaces each changed file, and
deletes each file of an app folder that the copy does not hold. It never touches a file that a
copy leaves out, such as `.env`. It merges `pdt.yml` and each `config.yml`: the copy's value wins
for each key it holds, and a key only this computer holds stays. Before it changes a file, it
saves the file in `.pdt/backup/<UTC time>/`. At a terminal it lists the changes and asks one
question; `--yes` and a run with no terminal do not ask. It stops in a Git clone, because Git
owns those files.

The pdt service starts `pdt sync down` in the project's folder, new or not. PDT_SERVICE_PROJECT
and PDT_SERVICE_CUSTOMER name the project and its customer, because a new folder has no pdt.yml
to read. After the copy, `pdt sync down` adds `managed_services:` to a pdt.yml that has none, as
`pdt register` does, and it refuses a copy whose pdt.yml names another customer.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import yaml

from pdt import config, console, copies, managed_services, register_cli, service_runner
from pdt.deploy import DECLINED
from pdt.utils import autoidm
from pdt.utils.email_auth import can_prompt

MERGED_FILES = {copies.ROOT: config.PROJECT_FILE, "app": "config.yml"}


def context() -> tuple[Path, str, str, autoidm.Registration]:
    """This project's folder, its name for AutoIDM, its customer, and that registration."""
    named = os.environ.get(service_runner.SERVICE_PROJECT_ENV, "").strip()
    customer = os.environ.get(service_runner.SERVICE_CUSTOMER_ENV, "").strip()
    if named and customer:
        root, name = Path.cwd(), named
    else:
        root = config.find_project()
        name = managed_services.backend_name(config.project_name())
        found = managed_services.settings()
        if found is None:
            raise config.ConfigError(
                f"{root / config.PROJECT_FILE} has no managed_services, so AutoIDM does not know "
                "this project. To add it, run `pdt register` in this project.")
        customer = found["customer"]
    registration = autoidm.registration(customer)
    if registration is None:
        raise config.ConfigError(f"This computer is not registered to AutoIDM for {customer}. "
                                 "To register it, run `pdt register`.")
    return root, name, customer, registration


def ask(question: str, assume_yes: bool) -> bool:
    """One `[y/N]` question at a terminal. `--yes` and a run with no terminal do not ask."""
    if assume_yes or not can_prompt(None):
        return True
    try:
        return console.confirm(question)
    except EOFError:
        return False


def chosen_units(root: Path, app: str | None) -> dict[str, list[dict]] | None:
    files, left_out = copies.collect(root)
    apps = copies.app_names(root)
    if app is not None and app not in apps:
        console.error(f"{console.value(root)} has no app folder {console.value(app)}.")
        return None
    units = copies.split_units(files, apps)
    if app is not None:
        units = {f"app={app}": units[f"app={app}"]}
    for path, why in left_out.items():
        console.detail(f"left out: {console.value(path)} ({console.escape(why)})", 2)
    return units


def up(app: str | None, assume_yes: bool) -> int:
    with config.project_lock():
        return send_up(app, assume_yes)


def send_up(app: str | None, assume_yes: bool) -> int:
    root, name, _customer, registration = context()
    console.heading(f"pdt will copy these files of {console.value(name)} to AutoIDM:")
    units = chosen_units(root, app)
    if units is None:
        return 1
    for unit, files in units.items():
        counts = Counter(entry["path"].split("/")[0] if "/" in entry["path"] else "."
                         for entry in files)
        console.bullet(f"{console.value(unit)}  {console.value(len(files))} "
                       f"file{'' if len(files) == 1 else 's'} in {console.value(len(counts))} "
                       f"folder{'' if len(counts) == 1 else 's'}")
    if not ask("Copy these files?", assume_yes):
        console.say("Cancelled. Nothing left this computer.")
        return DECLINED
    for unit, files in units.items():
        with console.waiting(f"sending: {unit} of {name}"):
            digest = copies.send(registration, name, unit, root, files)
        console.done(f"Copied {console.value(unit)} of {console.value(name)} to AutoIDM "
                     f"(SHA-256 {console.value(digest[:12])}).")
    return 0


def merged(local: dict, copy: dict) -> dict:
    """`local` with each key of `copy`: the copy's value wins, and a key only `local` holds
    stays. Mappings merge key by key."""
    result = dict(local)
    for key, value in copy.items():
        if isinstance(value, dict) and isinstance(local.get(key), dict):
            result[key] = merged(local[key], value)
        else:
            result[key] = value
    return result


def merged_text(local: bytes, copy: bytes, path: str) -> bytes:
    """The merged config file. When the merge changes nothing of the copy, the copy's text stays,
    with its comments."""
    try:
        mine = yaml.safe_load(local.decode("utf-8-sig")) or {}
        theirs = yaml.safe_load(copy.decode("utf-8-sig")) or {}
    except (yaml.YAMLError, UnicodeDecodeError) as e:
        raise copies.CopyError(f"{path} does not parse, so pdt cannot merge it: {e}") from None
    if not (isinstance(mine, dict) and isinstance(theirs, dict)):
        return copy
    both = merged(mine, theirs)
    if both == theirs:
        return copy
    return yaml.safe_dump(both, sort_keys=False, allow_unicode=True).encode("utf-8")


def plan_unit(root: Path, unit: str, staged: Path, files: list[dict]) -> dict[str, bytes | None]:
    """The new bytes of each path of the project that the unit changes, relative to `root`, and
    None for each path it deletes."""
    app = unit.removeprefix("app=") if unit != copies.ROOT else None
    merged_file = f"{app}/{MERGED_FILES['app']}" if app else MERGED_FILES[copies.ROOT]
    changes: dict[str, bytes | None] = {}
    wanted = set()
    for entry in files:
        wanted.add(entry["path"])
        new = Path(copies.long_path(staged / entry["path"])).read_bytes()
        target = copies.inside(root, entry["path"])
        try:
            current = Path(copies.long_path(target)).read_bytes()
        except FileNotFoundError:
            current = None
        if current is not None and entry["path"] == merged_file:
            new = merged_text(current, new, entry["path"])
        if current != new:
            changes[entry["path"]] = new
    if app and (root / app).is_dir():
        for entry in copies.collect(root / app)[0]:
            if f"{app}/{entry['path']}" not in wanted:
                changes[f"{app}/{entry['path']}"] = None
    return changes


def back_up(root: Path, paths: list[str]) -> Path | None:
    """Copy each existing file of `paths` to `.pdt/backup/<UTC time>/`."""
    existing = [path for path in paths if (root / path).is_file()]
    if not existing:
        return None
    folder = root / config.STATE_DIR / "backup" / datetime.now(timezone.utc).strftime(
        "%Y%m%dT%H%M%SZ")
    for path in existing:
        target = folder / path
        Path(copies.long_path(target.parent)).mkdir(parents=True, exist_ok=True)
        shutil.copy2(copies.long_path(root / path), copies.long_path(target))
    (root / config.STATE_DIR / ".gitignore").write_text("*\n", encoding="utf-8")
    return folder


def write_changes(root: Path, changes: dict[str, bytes | None]) -> None:
    for path, data in changes.items():
        target = copies.inside(root, path)
        if data is None:
            Path(copies.long_path(target)).unlink(missing_ok=True)
            continue
        Path(copies.long_path(target.parent)).mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(target.name + ".pdt-sync")
        Path(copies.long_path(temporary)).write_bytes(data)
        os.replace(copies.long_path(temporary), copies.long_path(target))


def down(app: str | None, assume_yes: bool) -> int:
    root, name, customer, registration = context()
    if copies.in_clone(root):
        console.error(f"{console.value(root)} is a Git clone, so {console.value('pdt sync down')} "
                      "changed nothing. Update this folder with Git.")
        return 1
    os.environ[config.LOCK_PROJECT_ENV] = str(root)
    with config.project_lock(copies.app_names(root)):
        return copy_down(root, name, customer, registration, app, assume_yes)


def copy_down(root: Path, name: str, customer: str, registration: autoidm.Registration,
              app: str | None, assume_yes: bool) -> int:
    units = copies.get(registration, name, f"app={app}" if app else None)
    units = [entry for entry in units if not entry["unit"].startswith("mr=")]
    if not units:
        what = f"app {app} of {name}" if app else name
        console.error(f"AutoIDM holds no copy of {console.value(what)}.")
        return 1
    changes: dict[str, bytes | None] = {}
    with tempfile.TemporaryDirectory(prefix="pdt-sync-") as folder:
        for entry in units:
            staged = Path(folder) / entry["unit"].replace("=", "-")
            with console.waiting(f"reading: {entry['unit']} of {name}"):
                files = copies.fetch(entry, staged)
            changes.update(plan_unit(root, entry["unit"], staged, files))
    if not (root / config.PROJECT_FILE).is_file() and config.PROJECT_FILE not in changes:
        console.error(f"The copy of {console.value(name)} holds no "
                      f"{console.value(config.PROJECT_FILE)}, so it is not a project.")
        return 1
    problem = copies.customer_problem(changes.get(config.PROJECT_FILE), customer)
    if problem:
        console.error(console.escape(problem[0].upper() + problem[1:] + "."))
        return 1
    if not changes:
        console.done(f"{console.value(name)} already holds the copy at AutoIDM.")
        return 0
    for path, data in sorted(changes.items()):
        label = "delete" if data is None else "change" if (root / path).is_file() else "add"
        console.say(f"{console.escape(label)}: {console.value(path)}")
    if not ask("Write these changes?", assume_yes):
        console.say("Cancelled. pdt changed nothing.")
        return DECLINED
    backup = back_up(root, list(changes))
    write_changes(root, changes)
    register_cli.save_customer(customer)
    if backup is not None:
        console.field("Saved the earlier files in", str(backup))
    console.done(f"{console.value(name)} now holds the copy at AutoIDM.")
    return 0
