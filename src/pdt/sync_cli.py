"""`pdt sync up|down|diff`: copy a project to AutoIDM, copy a snapshot back, and compare.

A snapshot is a zip of the project folder (pdt.snapshots). `.pdt/sync.json` records the last
snapshot this computer published or received: its ID and each file's hash. `pdt sync up --report`
sends a copy for a person to read, and it does not change that record. A CI installation sends
the merge request's files, and it does not change that record either.

`pdt sync down --snapshot ID` uses the record for three rules. It stops when no publication of the
snapshot has the record's ID as its parent, it deletes a file only when the record lists it and
the new snapshot does not, and it stops when a file it would overwrite or delete changed on this
computer since the record. `--force` passes the first and the last rule. Files are checked first,
and written after every check passes. It stops in a Git clone, because Git owns those files.

The pdt service starts `pdt sync down` in the project's folder, new or not. PDT_SERVICE_PROJECT
and PDT_SERVICE_CUSTOMER name the project and its customer, because a new folder has no pdt.yml
to read. After the copy, `pdt sync down` adds `managed_services:` to a pdt.yml that has none, as
`pdt register` does, and it refuses a snapshot whose pdt.yml names another customer.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from collections import Counter
from pathlib import Path

from pdt import config, console, managed_services, register_cli, service_runner, snapshots
from pdt.deploy import DECLINED
from pdt.utils import autoidm
from pdt.utils.email_auth import can_prompt

RECORD = "sync.json"


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
        return snapshots.content_hash(Path(snapshots.long_path(path)).read_bytes())
    except OSError:
        return None


def show_files(name: str, files: list[dict], left_out: dict[str, str]) -> None:
    console.heading(f"pdt will copy these files of {console.value(name)} to AutoIDM:")
    counts = Counter(entry["path"].split("/")[0] if "/" in entry["path"] else "."
                     for entry in files)
    for folder, count in sorted(counts.items()):
        console.bullet(f"{console.value(folder)}  {console.value(count)} "
                       f"file{'' if count == 1 else 's'}")
    for path, why in left_out.items():
        console.detail(f"left out: {console.value(path)} ({console.escape(why)})", 2)


def size_problem(name: str, files: list[dict]) -> str:
    """Why /v1/snapshots/start cannot take this list, or "". Each entry grows in the JSON form."""
    if len(json.dumps({"files": files})) > snapshots.MAX_BODY_BYTES - 1024:
        return (f"{console.value(name)} holds {console.value(len(files))} files, and AutoIDM "
                "takes a list of about 2000 files. Delete files the project does not need, "
                "then run the command again.")
    return ""


def up(assume_yes: bool, ref: str | None, label: str | None, report: bool = False) -> int:
    root, name, customer, registration = context()
    files, left_out = snapshots.collect(root)
    if not files:
        console.error(f"{console.value(root)} holds no file to send.")
        return 1
    for text in (ref, label):
        if text is not None and len(text) > snapshots.MAX_NOTE_CHARACTERS:
            console.error(f"--ref and --label each hold at most "
                          f"{console.value(snapshots.MAX_NOTE_CHARACTERS)} characters.")
            return 1
    problem = size_problem(name, files)
    if problem:
        console.error(problem)
        return 1
    show_files(name, files, left_out)
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
    from_ci = registration.source == autoidm.ID_ENV
    purpose = "merge_request" if from_ci else "report" if report else "publish"
    parent = read_record(root).get("snapshot_id")
    with tempfile.TemporaryDirectory(prefix="pdt-sync-") as folder:
        with console.waiting(f"sending: snapshot of {name}"):
            identity = snapshots.publish(registration, customer, name, root, files, purpose, ref,
                                         label, parent, Path(folder))
    if purpose == "publish":
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


def show_heads(registration: autoidm.Registration, name: str) -> int:
    """`pdt sync down` with no ID: it copies nothing, because a computer takes one named version."""
    found = snapshots.heads(registration, name)
    if not found:
        console.error(f"AutoIDM holds no snapshot of {console.value(name)}.")
        return 1
    console.error(f"{console.value('pdt sync down')} needs the ID of a snapshot of "
                  f"{console.value(name)}. These are the newest:")
    console.table(["snapshot", "installation", "time"],
                  [[head["snapshot_id"], str(head.get("installation_id") or "-"),
                    str(head.get("at") or "-")] for head in found], ["bold"])
    console.next_steps([(f"pdt sync down --snapshot {found[0]['snapshot_id']}",
                         "copy the newest snapshot into this project")])
    return 1


def parent_problem(known_id: object, identity: str, publications: list) -> bool:
    if not known_id or known_id == identity:
        return False
    return not any(isinstance(item, dict) and item.get("parent") == known_id
                   for item in publications)


def down(wanted: str | None, force: bool) -> int:
    root, name, customer, registration = context()
    if wanted is None:
        return show_heads(registration, name)
    if snapshots.in_clone(root):
        console.error(f"{console.value(root)} is a Git clone, so {console.value('pdt sync down')} "
                      "changed nothing. Update this folder with Git.")
        return 1
    with tempfile.TemporaryDirectory(prefix="pdt-sync-") as folder:
        staged = Path(folder) / "files"
        staged.mkdir()
        with console.waiting(f"reading: snapshot of {name}"):
            identity, files, publications = snapshots.fetch(registration, name, wanted, staged,
                                                            customer)
        if not (root / config.PROJECT_FILE).is_file() and not (staged / config.PROJECT_FILE).is_file():
            console.error(f"Snapshot {console.value(identity)} holds no "
                          f"{console.value(config.PROJECT_FILE)}, so it is not a project.")
            return 1
        record = read_record(root)
        if parent_problem(record.get("snapshot_id"), identity, publications) and not force:
            console.error(f"This computer holds snapshot {console.value(record['snapshot_id'])}, "
                          f"and no publication of snapshot {console.value(identity)} names it as "
                          f"the parent, so {console.value('pdt sync down')} changed nothing.")
            console.next_steps([(f"pdt sync down --snapshot {identity} --force",
                                 "copy this snapshot over the current files")])
            return 1
        known = record.get("files") or {}
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
    register_cli.save_customer(customer)
    # The customer line is pdt's own edit, so a later sync does not read it as a change here.
    write_record(root, identity, [
        {**entry, "sha256": file_hash(root / entry["path"])}
        if entry["path"] == config.PROJECT_FILE else entry for entry in files])
    for label, paths in (("added", added), ("changed", changed), ("deleted", deleted)):
        for path in paths:
            console.say(f"{console.escape(label)}: {console.value(path)}")
    if added or changed or deleted:
        console.done(f"{console.value(name)} now holds snapshot {console.value(identity)}.")
    else:
        console.done(f"{console.value(name)} already holds snapshot {console.value(identity)}.")
    return 0


def diff(as_json: bool) -> int:
    root, name, _customer, registration = context()
    found = snapshots.heads(registration, name)
    if not found:
        console.error(f"AutoIDM holds no snapshot of {console.value(name)} to compare with.")
        return 1
    identity, files = snapshots.read_manifest(registration, name, found[0]["snapshot_id"])
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
