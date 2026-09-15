"""CLI helpers behind `pdt storage`, called by every provider's storage command.

Each function takes a `pdt.utils.storage.Store` already scoped to one app.
"""

from __future__ import annotations

import os
import platform
import re
import socket
import subprocess
import tempfile
import threading
from datetime import timedelta
from pathlib import Path

from pdt import config, console
from pdt.utils.storage import Local, StorageConflict, StorageLocked

USAGE = ("usage: pdt storage <app> ls [path] | get <path> [dest] | query <sql> "
         "| mount [dir] [--read-only] | destroy [--yes]")
LOCK_RENEW = timedelta(minutes=10)


def ls(store, path) -> int:
    fs = store.fs()
    try:
        entries = sorted(fs.ls(path, detail=True), key=lambda entry: entry["name"])
    except FileNotFoundError:
        entries = []
    if not entries:
        console.say(f"nothing under {path or '/'}")
        return 0
    console.table(["Path", "Size"], [[entry["name"], entry["size"]] for entry in entries])
    return 0


def get(store, path, dest: Path) -> int:
    store.fs().get(path, str(dest))
    console.done(f"saved {path} to {dest}")
    return 0


def query(store, sql) -> int:
    import duckdb

    fs = store.fs()
    with tempfile.TemporaryDirectory(prefix="pdt-storage-") as tmp:
        rewritten = sql
        for pattern in set(re.findall(r"'([^']+)'", sql)):
            matches = fs.glob(pattern)
            if not matches:
                console.error(f"no objects match {pattern!r}")
                return 1
            for match in matches:
                local_path = Path(tmp) / match
                local_path.parent.mkdir(parents=True, exist_ok=True)
                fs.get(match, str(local_path))
            rewritten = rewritten.replace(f"'{pattern}'", f"'{Path(tmp) / pattern}'")
        result = duckdb.sql(rewritten)
        console.table([column[0] for column in result.description], result.fetchall())
    return 0


def mount(store, app, args, assume_yes) -> int:
    from pdt import rclone

    read_only = "--read-only" in args
    words = [arg for arg in args if arg != "--read-only"]
    backend = store.backend()
    if isinstance(backend, Local):
        console.done(f"{app}'s files are already a folder on this computer: {backend.folder}")
        return 0
    target = Path(words[0]) if words else find_mount_folder(app)
    try:
        binary = rclone.ensure_rclone(assume_yes)
        rclone.ensure_winfsp(assume_yes)
    except rclone.RcloneError as e:
        console.error(str(e))
        return 1
    lock = None
    if not read_only:
        try:
            lock = store.take_lock(owner=f"mount on {socket.gethostname()}")
        except StorageLocked as e:
            console.warn(f"{e}; mounting read-only")
            read_only = True
    remote, env = backend.rclone_remote()
    command = mount_command(binary, remote, target, read_only)
    stop = threading.Event()
    renewer = threading.Thread(target=renew, args=(store, lock, stop), daemon=True)
    renewer.start()
    if os.name != "nt":
        target.mkdir(parents=True, exist_ok=True)
    console.status(f"mounting {app} at {target}" + (" (read-only)" if read_only else ""))
    console.say("Press Ctrl-C to unmount.")
    proc = subprocess.Popen(command, env=dict(os.environ, **env))
    interrupted = False
    try:
        proc.wait()
    except KeyboardInterrupt:
        interrupted = True
        proc.terminate()
        proc.wait()
    finally:
        stop.set()
        if lock is not None:
            store.release_lock(lock)
    if proc.returncode and not interrupted:
        console.error(f"rclone ended with code {proc.returncode}")
        return 1
    console.done(f"unmounted {target}")
    return 0


def find_mount_folder(app: str) -> Path:
    return config.find_project() / ".pdt" / "mount" / app


def mount_command(binary: str, remote: str, target: Path, read_only: bool) -> list[str]:
    # macOS mounts through its own NFS client, so no kernel extension is
    # needed. Linux and Windows use FUSE (fuse3 and WinFsp).
    verb = "nfsmount" if platform.system() == "Darwin" else "mount"
    command = [binary, verb, remote, str(target),
               "--vfs-cache-mode", "full", "--dir-cache-time", "10s"]
    if read_only:
        command.append("--read-only")
    return command


def renew(store, lock, stop: threading.Event) -> None:
    while lock is not None and not stop.wait(LOCK_RENEW.total_seconds()):
        try:
            lock.update(store.renew_lock(lock))
        except StorageConflict as e:
            console.warn(f"{e}; a run may change state/ while it is mounted")
            return


def destroy(store, app, assume_yes) -> int:
    from pdt import deploy

    fs = store.fs()
    objects = fs.find("")
    if not objects:
        console.say(f"no objects under {app}/")
        return 0
    if not deploy.confirm([f"delete {len(objects)} object(s) under {app}/"], assume_yes):
        return 1
    fs.rm(objects)
    console.done(f"deleted {len(objects)} object(s) under {app}/")
    return 0


def run(store, app: dict, rest: list[str], assume_yes: bool) -> int:
    name = app["name"]
    if not app["storage"]:
        console.error(f"storage is turned off for {name}; "
                      f"remove storage: false from {name}/config.yml")
        return 1
    handlers = {
        "ls": (0, lambda args: ls(store, args[0] if args else "")),
        "get": (1, lambda args: get(store, args[0],
                                    Path(args[1] if len(args) > 1 else Path(args[0]).name))),
        "query": (1, lambda args: query(store, args[0])),
        "mount": (0, lambda args: mount(store, name, args, assume_yes)),
        "destroy": (0, lambda args: destroy(store, name, assume_yes)),
    }
    subcommand, *args = rest or [""]
    if subcommand not in handlers:
        console.error(USAGE)
        return 1
    needed, handler = handlers[subcommand]
    if len(args) < needed:
        console.error(USAGE)
        return 1
    return handler(args)
