"""Bring a project that an older pdt release made up to this pdt.

Each Migration names the pdt release that made a change to what a project
holds, and a plan function. The plan function reads the project and returns
a Plan. It writes nothing, and it returns no writes when the project already
has the change, so a second run changes nothing.

`migrated_by` in the project pdt.yml names the release of the newest
migration the project has; a project without it has none. At the start of
each command that needs a project, cli.main calls `start`, which runs every
migration of a newer release in MIGRATIONS order. Before it changes a file,
it copies the file into .pdt/backups/<UTC time>/. It sets `migrated_by`
after the last migration of each release. A deployed job never runs this
module, so a job that reads a newer pdt.yml keeps running.
"""

from __future__ import annotations

import contextlib
import difflib
import hashlib
import json
import re
import shutil
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from itertools import groupby
from pathlib import Path
from typing import Callable

import yaml
from dotenv import dotenv_values

from pdt import __version__, config, console, scaffold
from pdt.config import APP_FILE, PROJECT_FILE, STATE_DIR, ConfigError
from pdt.deploy_common import DOCKERFILE
from pdt.utils.env_secret import private_file

BACKUPS = f"{STATE_DIR}/backups"
KEEP_BACKUPS = 5
# These commands go on, with a note, when a migration needs the user first,
# so a user can still read the logs of a job that failed.
READ_ONLY_COMMANDS = ("list", "validate", "runs", "logs", "health")
MIGRATED_BY_COMMENT = "pdt writes this line when it updates the project. Do not change it."
# The sha256 of each text that a release wrote, and the first release that wrote
# it. A file that still has one of these texts has no edits by the user.
SHIPPED = {
    "AGENTS.md": {
        "bafca438fb721383ae7cc3931407bb9e4f2e1a914aad1c9f6a16646806d5a0c9": "0.1.1",
        "22527471ebfcbdde9f1a6efee6bbf0beeeda077931c9cd11a117baf501ff8bf9": "0.1.3",
        "72f6d05cb2c16c8e01a6a98085639b51cc1d79682ab5bc2dcd30e17ee78fde49": "0.1.4",
        "8befcd4c6e2fb5d31f396416d2662da07df176fa5fcccb7b72374b5e0dd331e4": "0.1.6",
        "2d42cee1a02ef8ddff652cc70f876c6ebc5521609843c59d821aeb32663457e3": "0.1.7",
    },
    "CLAUDE.md": {
        "336cc4fbf19beaada7ccf9986414fa91851a8d7a07dfb3ccbe800a69eed0ab49": "0.1.1",
    },
}
OLD_RUNTIMES = {"lambda": "AWS Lambda function", "functions": "Azure Function App"}
OLD_ENTRYPOINT = 'ENTRYPOINT ["uv", "run", "--script", "run.py"]'
ENTRYPOINT = next(line for line in DOCKERFILE.splitlines() if line.startswith("ENTRYPOINT"))
ENVIRONMENT_LINE = re.compile(
    r"^(\s*(?:export\s+)?PDT_AZURE_CONTAINER_APPS_ENVIRONMENT\s*=\s*)(['\"]?)([^'\"#\s]*)\2(.*)$")


@dataclass(frozen=True)
class Plan:
    """What one migration changes in a project.

    `writes` maps a path in the project to its new text, or to None to remove
    the file. Each of `steps` is a thing the user must do first; any step
    stops the run, and pdt writes nothing for that migration. `notes` print
    after the run. `edited` maps a generated file that the user edited to the
    text this pdt would write: pdt leaves the file and saves the text in the
    backup folder.
    """

    writes: dict[str, str | None] = field(default_factory=dict)
    steps: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    edited: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Migration:
    """`title` follows the path of each file the migration writes, as in
    "pdt.yml no longer sets platform.runtime"."""

    release: str
    title: str
    plan: Callable[[Path], Plan]


class Blocked(ConfigError):
    pass


def retired_runtime(project: Path) -> Plan:
    """0.1.1: every cloud job runs as a container, so `platform.runtime` went away."""
    writes, steps = {}, []
    for rel in [PROJECT_FILE, *(f"{name}/{APP_FILE}" for name in config.app_folders(project))]:
        path = project / rel
        if not path.is_file():
            continue
        text = path.read_text()
        try:
            data = yaml.safe_load(text)
        except yaml.YAMLError:
            continue
        if not isinstance(data, dict):
            continue
        app = "<app>" if rel == PROJECT_FILE else rel.split("/")[0]
        places = [(("platform", "runtime"), app)]
        if rel == PROJECT_FILE and isinstance(data.get("apps"), list):
            places += [(("apps", index, "platform", "runtime"), entry.get("name", "<app>"))
                       for index, entry in enumerate(data["apps"]) if isinstance(entry, dict)]
        new = text
        for key_path, app in places:
            found = config.yaml_key(new, key_path)
            if found is None:
                continue
            value = found[1].value
            if isinstance(value, str) and value in OLD_RUNTIMES:
                steps += [f"If pdt 0.1.0 deployed {app}, the {OLD_RUNTIMES[value]} it made is still "
                          f"there. Remove it: uvx --from pdt-cli==0.1.0 pdt destroy {app}",
                          f"Delete the line `runtime: {value}` from {rel}."]
                continue
            removed = config.remove_key(new, key_path)
            if removed is None:
                steps.append(f"Delete `runtime: {value}` under platform: in {rel}.")
            else:
                new = removed
        if new != text:
            writes[rel] = new
    return Plan(writes=writes, steps=steps)


def azure_environment(project: Path) -> Plan:
    """0.1.2: PDT_AZURE_CONTAINER_APPS_ENVIRONMENT names <resource-group>/<name>."""
    try:
        platform = config.load_yaml(project / PROJECT_FILE).get("platform")
    except ConfigError:
        platform = None
    if not isinstance(platform, dict):
        platform = {}
    writes = {}
    for rel in [".env", *(f"{name}/.env" for name in config.app_folders(project))]:
        path = project / rel
        if not path.is_file():
            continue
        group = (dotenv_values(path).get("PDT_AZURE_RESOURCE_GROUP")
                 or platform.get("resource_group") or "pdt")
        lines = path.read_text().splitlines(keepends=True)
        changed = False
        for index, line in enumerate(lines):
            match = ENVIRONMENT_LINE.match(line.rstrip("\n"))
            if match is None or match[3] == "" or "/" in match[3]:
                continue
            ending = "\n" if line.endswith("\n") else ""
            lines[index] = f"{match[1]}{match[2]}{group}/{match[3]}{match[2]}{match[4]}{ending}"
            changed = True
        if changed:
            writes[rel] = "".join(lines)
    return Plan(writes=writes)


def dockerfile_exit_line(project: Path) -> Plan:
    """0.1.3: a cloud job's log ends with `pdt: exit N`, which `pdt runs` reads."""
    writes, notes = {}, []
    for name in config.app_folders(project):
        rel = f"{name}/Dockerfile"
        path = project / rel
        if not path.is_file():
            continue
        text = path.read_text()
        if "pdt: exit" in text:
            continue
        lines = text.splitlines(keepends=True)
        old = [index for index, line in enumerate(lines) if " ".join(line.split()) == OLD_ENTRYPOINT]
        if old:
            writes[rel] = "".join(ENTRYPOINT + "\n" if index in old else line
                                  for index, line in enumerate(lines))
            notes.append(f"pdt deploy {name}    the deployed job uses the old Dockerfile until you "
                         "deploy it")
        else:
            notes.append(f"{rel} must print `pdt: exit <code>` as the last line of each run, or "
                         "`pdt runs` cannot tell whether a run worked. Copy the ENTRYPOINT line "
                         f"that pdt uses: {ENTRYPOINT}")
    return Plan(writes=writes, notes=notes)


def gitignore(project: Path) -> Plan:
    """0.1.1 and 0.1.3: the lines `pdt init` writes into .gitignore."""
    path = project / ".gitignore"
    text = path.read_text() if path.is_file() else ""
    have = {line.strip() for line in text.splitlines()}
    missing = [line for line in scaffold.GITIGNORE_TEXT.splitlines() if line not in have]
    if not missing:
        return Plan()
    joined = text if text == "" or text.endswith("\n") else text + "\n"
    return Plan(writes={".gitignore": joined + "".join(f"{line}\n" for line in missing)})


def generated_file(project: Path, rel: str, text: str) -> Plan:
    path = project / rel
    if not path.is_file():
        return Plan(writes={rel: text})
    have = path.read_text()
    if have == text:
        return Plan()
    if hashlib.sha256(have.encode()).hexdigest() in SHIPPED[rel]:
        return Plan(writes={rel: text})
    return Plan(edited={rel: text})


def agents_file(project: Path) -> Plan:
    """Each release that changed AGENTS_TEXT: the text for an AI agent."""
    return generated_file(project, "AGENTS.md", scaffold.AGENTS_TEXT)


def claude_file(project: Path) -> Plan:
    """0.1.1: CLAUDE.md points Claude Code at AGENTS.md."""
    return generated_file(project, "CLAUDE.md", scaffold.CLAUDE_TEXT)


AGENTS_TITLE = f"has the text for pdt {__version__}"
GITIGNORE_TITLE = "names the files that git must not store"

# The run order. Register agents_file again for each release that changes
# AGENTS_TEXT, with the new text's hash in SHIPPED.
MIGRATIONS: tuple[Migration, ...] = (
    Migration("0.1.1", "no longer sets platform.runtime", retired_runtime),
    Migration("0.1.1", GITIGNORE_TITLE, gitignore),
    Migration("0.1.1", "points Claude Code at AGENTS.md", claude_file),
    Migration("0.1.1", AGENTS_TITLE, agents_file),
    Migration("0.1.2", "names the Azure environment as <resource group>/<name>", azure_environment),
    Migration("0.1.3", "prints `pdt: exit <code>` at the end of each run", dockerfile_exit_line),
    Migration("0.1.3", GITIGNORE_TITLE, gitignore),
    Migration("0.1.3", AGENTS_TITLE, agents_file),
    Migration("0.1.4", AGENTS_TITLE, agents_file),
    Migration("0.1.6", AGENTS_TITLE, agents_file),
    Migration("0.1.7", AGENTS_TITLE, agents_file),
)


def version(release: str) -> tuple[int, ...]:
    return tuple(int(part) for part in release.split("."))


def migrated_by(project: Path) -> str | None:
    value = config.load_yaml(project / PROJECT_FILE).get("migrated_by")
    if value is None:
        return None
    if re.fullmatch(r"\d+(\.\d+)*", str(value)) is None:
        raise ConfigError(f"{PROJECT_FILE}: migrated_by: {value!r} is not a pdt release. "
                          "Delete the line, and pdt writes it again.")
    return str(value)


def pending(project: Path) -> list[Migration]:
    """The migrations the project does not have. Stops when a newer pdt updated it."""
    have = migrated_by(project)
    if have is None:
        return list(MIGRATIONS)
    if version(have) > version(MIGRATIONS[-1].release):
        raise ConfigError(f"this project needs pdt {have} or newer. You have pdt {__version__}.\n"
                          "Update pdt, then run the command again:\n"
                          "  uv tool upgrade pdt-cli          (or: winget upgrade AutoIDM.pdt)")
    return [migration for migration in MIGRATIONS if version(migration.release) > version(have)]


def blocked(steps: list[str]) -> Blocked:
    lines = [f"pdt {__version__} must change this project, but it cannot do it alone. "
             "It did not make this change."]
    for number, step in enumerate([*steps, "Run your pdt command again."], start=1):
        lines.append(f"  {number}. {step}")
    return Blocked("\n".join(lines))


def start(command: str, json_output: bool = False) -> None:
    """Bring the project up to date before `command` runs. With `json_output` the lines
    go to stderr, so stdout holds only the command's JSON."""
    try:
        project = config.find_project()
    except ConfigError:
        return
    with contextlib.redirect_stdout(sys.stderr) if json_output else contextlib.nullcontext():
        try:
            bring_up_to_date(project)
        except Blocked as e:
            if command not in READ_ONLY_COMMANDS:
                raise
            console.note(str(e))


def bring_up_to_date(project: Path) -> None:
    if not pending(project):
        return
    with config.locked(project / PROJECT_FILE):
        run(project, pending(project))


def run(project: Path, todo: list[Migration]) -> None:
    before = after = migrated_by(project)
    backup: Path | None = None
    saved: dict[str, bool] = {}
    changes: list[str] = []
    edited: list[str] = []
    notes: list[str] = []
    finished = False

    def save_originals(rels) -> None:
        for rel in rels:
            if rel in saved:
                continue
            source = project / rel
            saved[rel] = source.is_file()
            if source.is_file():
                target = backup / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                if target.name.startswith(".env"):
                    private_file(target)
                shutil.copyfile(source, target)

    try:
        for release, group in groupby(todo, key=lambda migration: migration.release):
            for migration in group:
                plan = migration.plan(project)
                if plan.steps:
                    raise blocked(plan.steps)
                if backup is None and (plan.writes or plan.edited):
                    backup = new_backup(project)
                for rel, text in plan.edited.items():
                    target = backup / f"{rel}.new"
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text(text)
                    edited.append(rel)
                save_originals(plan.writes)
                for rel, text in plan.writes.items():
                    if not changes:
                        console.heading(f"pdt {__version__} updated this project:")
                    path = project / rel
                    if text is None:
                        path.unlink(missing_ok=True)
                    else:
                        path.parent.mkdir(parents=True, exist_ok=True)
                        config.write_text_atomically(path, text)
                    changes.append(f"{rel} {migration.title}")
                    console.bullet(changes[-1])
                notes.extend(plan.notes)
            if backup is not None:
                save_originals([PROJECT_FILE])
            path = project / PROJECT_FILE
            config.write_text_atomically(path, config.set_top_level_key(
                path.read_text(), "migrated_by", release, MIGRATED_BY_COMMENT))
            after = release
        finished = True
    except Blocked:
        finished = True
        raise
    finally:
        if backup is not None:
            manifest = {"pdt": __version__, "migrated_by_before": before,
                        "migrated_by_after": after, "changes": changes,
                        "files": saved}
            (backup / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
            where = backup.relative_to(project).as_posix()
            if any(saved.values()):
                console.bullet(f"The files as they were are in {where}/.")
            for rel in dict.fromkeys(edited):
                console.note(f"{rel} has your own edits, so pdt left it as it is. "
                             f"The text for pdt {__version__} is in {where}/{rel}.new.")
            if not finished:
                console.note("pdt stopped before it finished updating this project. Run the "
                             f"command again to finish, or copy the files in {where}/ back "
                             "into the project to undo the change.")
            remove_old_backups(project)
        if notes and finished:
            console.heading("Next steps:")
            for note in dict.fromkeys(notes):
                console.bullet(note)
        if changes or edited or notes:
            console.say()


def new_backup(project: Path) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")
    folder = project / BACKUPS / stamp
    number = 1
    while folder.exists():
        number += 1
        folder = project / BACKUPS / f"{stamp}-{number}"
    folder.mkdir(parents=True)
    (project / STATE_DIR / ".gitignore").write_text("*\n")
    return folder


def remove_old_backups(project: Path) -> None:
    folders = sorted(path for path in (project / BACKUPS).iterdir() if path.is_dir())
    for folder in folders[:-KEEP_BACKUPS]:
        shutil.rmtree(folder)


def migrate(project: Path, dry_run: bool) -> int:
    """`pdt migrate`: run the migrations, or with `dry_run` print them as a diff."""
    if not dry_run:
        bring_up_to_date(project)
        console.done(f"This project is up to date for pdt {__version__}.")
        return 0
    have = migrated_by(project)
    reached = have
    final: dict[str, str | None] = {}
    steps: list[str] = []
    for release, group in groupby(pending(project), key=lambda migration: migration.release):
        for migration in group:
            plan = migration.plan(project)
            if plan.steps:
                steps = plan.steps
                break
            final.update(plan.writes)
        if steps:
            break
        reached = release
    if reached != have:
        text = final.get(PROJECT_FILE) or (project / PROJECT_FILE).read_text()
        final[PROJECT_FILE] = config.set_top_level_key(text, "migrated_by", reached,
                                                       MIGRATED_BY_COMMENT)
    changed = False
    for rel in sorted(final):
        path = project / rel
        old = path.read_text() if path.is_file() else ""
        new = final[rel] or ""
        for line in difflib.unified_diff(old.splitlines(keepends=True), new.splitlines(keepends=True),
                                         f"a/{rel}", f"b/{rel}"):
            console.say(line.rstrip("\n"))
            changed = True
    if steps:
        raise blocked(steps)
    if not changed:
        console.done(f"This project is up to date for pdt {__version__}.")
        return 0
    return 1
