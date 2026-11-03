"""`pdt sync up|down|diff`: copy a project to AutoIDM, copy a snapshot back, and compare.

A snapshot is a zip of the project folder (pdt.snapshots). `.pdt/sync.json` records the last
snapshot this computer sent or received: its ID and each file's hash. `pdt sync down` uses the
record for two rules: it deletes a file only when the record lists it and the new snapshot does
not, and it stops when a file it would overwrite or delete changed on this computer since the
record. Files are checked first, and written after every check passes.

The pdt service starts `pdt sync down` in a new folder for a project this computer does not
hold yet. PDT_SERVICE_PROJECT and PDT_SERVICE_CUSTOMER then name it, because the folder has no
pdt.yml to read.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from collections import Counter
from pathlib import Path

from pdt import config, console, managed_services, service_runner, snapshots
from pdt.deploy import DECLINED
from pdt.utils import autoidm
from pdt.utils.email_auth import can_prompt

RECORD = "sync.json"


def context() -> tuple[Path, str, autoidm.Registration]:
    """This project's folder, its name for AutoIDM, and the registration of its customer."""
    named = os.environ.get(service_runner.SERVICE_PROJECT_ENV, "").strip()
    customer = os.environ.get(service_runner.SERVICE_CUSTOMER_ENV, "").strip()
    if named and customer and not (Path.cwd() / config.PROJECT_FILE).is_file():
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
    return root, name, registration


def read_record(root: Path) -> dict:
    try:
        found = json.loads((root / config.STATE_DIR / RECORD).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return found if isinstance(found, dict) else {}


def write_record(root: Path, identity: str, files: list[dict]) -> None:
    folder = root / config.STATE_DIR
    folder.mkdir(exist_ok=True)
    (folder / ".gitignore").write_text("*\n", encoding="utf-8")
    config.write_text_atomically(folder / RECORD, json.dumps({
        "snapshot_id": identity, "files": {entry["path"]: entry["sha256"] for entry in files},
    }, indent=2) + "\n")


def file_hash(path: Path) -> str | None:
    try:
        with open(snapshots.long_path(path), "rb") as handle:
            return hashlib.file_digest(handle, "sha256").hexdigest()
    except OSError:
        return None


def up(assume_yes: bool, ref: str | None, label: str | None) -> int:
    root, name, registration = context()
    files, large = snapshots.collect(root)
    if not files:
        console.error(f"{console.value(root)} holds no file to send.")
        return 1
    for text in (ref, label):
        if text is not None and len(text) > snapshots.MAX_NOTE_CHARACTERS:
            console.error(f"--ref and --label each hold at most "
                          f"{console.value(snapshots.MAX_NOTE_CHARACTERS)} characters.")
            return 1
    identity = snapshots.snapshot_id(files)
    manifest = {"files": files, "ref": ref, "label": label}
    if len(json.dumps(manifest)) > snapshots.MAX_BODY_BYTES - 1024:
        console.error(f"{console.value(name)} holds {console.value(len(files))} files, and AutoIDM "
                      "takes a list of about 2000 files. Delete files the project does not need, "
                      "then run the command again.")
        return 1
    console.heading(f"pdt will copy these files of {console.value(name)} to AutoIDM:")
    counts = Counter(entry["path"].split("/")[0] if "/" in entry["path"] else "."
                     for entry in files)
    for folder, count in sorted(counts.items()):
        console.bullet(f"{console.value(folder)}  {console.value(count)} "
                       f"file{'' if count == 1 else 's'}")
    console.detail(f"Never copied: {console.value('.env')}, keys, {console.value('.pdt')}, "
                   "and each link.", 2)
    for path in large:
        console.warn(f"Left out {console.value(path)}: it is larger than "
                     f"{console.value(console.size(snapshots.MAX_FILE_BYTES))}.")
    if not assume_yes:
        if not can_prompt(None):
            console.warn("There is no one to answer. Run this in a terminal, or add --yes.")
            return 1
        try:
            if not console.confirm("Copy these files?"):
                console.say("Cancelled. Nothing left this computer.")
                return DECLINED
        except EOFError:
            return DECLINED
    with tempfile.TemporaryDirectory(prefix="pdt-sync-") as folder:
        archive = Path(folder) / f"{identity}.zip"
        snapshots.build_zip(root, files, archive)
        size = archive.stat().st_size
        if size > snapshots.MAX_SNAPSHOT_BYTES:
            console.error(f"The zip of {console.value(name)} is {console.value(console.size(size))}, "
                          f"over the "
                          f"{console.value(console.size(snapshots.MAX_SNAPSHOT_BYTES))} limit.")
            return 1
        answer = autoidm.post(registration, "/v1/snapshots/start", {
            "version": 1, "project": name, "snapshot_id": identity, "size": size,
            "manifest": manifest})
        place = answer.get("upload")
        if place:
            with console.waiting(f"sending: snapshot of {name}"):
                autoidm.post_file(place, archive.name, archive.read_bytes(), "/v1/snapshots/start")
        else:
            console.status("AutoIDM already holds these files.")
    autoidm.post(registration, "/v1/snapshots/finish",
                 {"version": 1, "project": name, "snapshot_id": identity})
    write_record(root, identity, files)
    console.done(f"Copied {console.value(name)} to AutoIDM as snapshot {console.value(identity)}.")
    return 0


def plan_down(root: Path, files: list[dict], known: dict) -> tuple[list[str], list[str], list[str],
                                                                  list[str]]:
    """The added, changed, and deleted paths, and each path whose change on this computer the
    sync would lose: it differs from the record, or the record does not know it."""
    wanted = {entry["path"]: entry["sha256"] for entry in files}
    added, changed, conflicts = [], [], []
    for path, digest in sorted(wanted.items()):
        local = file_hash(snapshots.inside(root, path))
        if local == digest:
            continue
        if local is None:
            added.append(path)
            continue
        changed.append(path)
        if local != known.get(path):
            conflicts.append(path)
    deleted = []
    for path in sorted(set(known) - set(wanted)):
        if snapshots.path_problem(path):
            continue
        target = snapshots.inside(root, path)
        local = file_hash(target)
        if local is None:
            continue
        deleted.append(path)
        if local != known[path]:
            conflicts.append(path)
    return added, changed, deleted, sorted(conflicts)


def down(wanted: str | None, force: bool) -> int:
    root, name, registration = context()
    with tempfile.TemporaryDirectory(prefix="pdt-sync-") as folder:
        staged = Path(folder) / "files"
        staged.mkdir()
        with console.waiting(f"reading: snapshot of {name}"):
            identity, files = snapshots.fetch(registration, name, wanted, staged)
        known = read_record(root).get("files") or {}
        added, changed, deleted, conflicts = plan_down(root, files, known)
        if conflicts and not force:
            console.error(f"These files changed on this computer since the last snapshot, so "
                          f"{console.value('pdt sync down')} changed nothing:")
            for path in conflicts:
                console.name(path)
            console.next_steps([("pdt sync diff", "show what changed"),
                                (f"pdt sync down --snapshot {identity} --force",
                                 "overwrite these files with the snapshot")])
            return 1
        for path in [*added, *changed]:
            target = snapshots.inside(root, path)
            Path(snapshots.long_path(target.parent)).mkdir(parents=True, exist_ok=True)
            temporary = target.with_name(target.name + ".pdt-sync")
            shutil.copyfile(snapshots.long_path(staged / path), snapshots.long_path(temporary))
            os.replace(snapshots.long_path(temporary), snapshots.long_path(target))
        for path in deleted:
            Path(snapshots.long_path(snapshots.inside(root, path))).unlink(missing_ok=True)
    write_record(root, identity, files)
    for label, paths in (("added", added), ("changed", changed), ("deleted", deleted)):
        for path in paths:
            console.say(f"{console.escape(label)}: {console.value(path)}")
    autoidm.post(registration, "/v1/snapshots/finish",
                 {"version": 1, "project": name, "snapshot_id": identity})
    if added or changed or deleted:
        console.done(f"{console.value(name)} now holds snapshot {console.value(identity)}.")
    else:
        console.done(f"{console.value(name)} already holds snapshot {console.value(identity)}.")
    return 0


def diff(as_json: bool) -> int:
    root, name, registration = context()
    identity, files = snapshots.read_manifest(registration, name, None)
    local = {entry["path"]: entry["sha256"] for entry in snapshots.collect(root)[0]}
    head = {entry["path"]: entry["sha256"] for entry in files}
    added = sorted(set(local) - set(head))
    removed = sorted(set(head) - set(local))
    changed = sorted(path for path in set(local) & set(head) if local[path] != head[path])
    if as_json:
        console.data(json.dumps({"snapshot_id": identity, "added": added, "removed": removed,
                                 "changed": changed}))
        return 0
    console.field("Compared with snapshot", identity)
    for label, paths in (("only here", added), ("only in the snapshot", removed),
                         ("changed", changed)):
        for path in paths:
            console.say(f"{console.escape(label)}: {console.value(path)}")
    if not (added or removed or changed):
        console.done("This folder matches the snapshot.")
    return 0
