"""CLI helpers behind `pdt storage`, called by every provider's storage command.

Each function takes a `pdt.utils.storage.Store` already scoped to one app.
"""

from __future__ import annotations

import json
import re
import tempfile
from pathlib import Path

from pdt import console

COMMANDS = ("ls", "get", "query", "unlock", "destroy")
USAGE = ("usage: pdt storage <app> ls [path] [--recursive] [--json] | get <path> [dest] "
         "| query <sql> | unlock [--yes] | destroy [--yes]")


def ls(store, path, as_json: bool = False, recursive: bool = False) -> int:
    fs = store.fs()
    try:
        if recursive:
            entries = [dict(entry, name=name) for name, entry in
                       sorted(fs.find(path, detail=True).items())]
        else:
            entries = sorted(fs.ls(path, detail=True), key=lambda entry: entry["name"])
    except FileNotFoundError:
        entries = []
    if as_json:
        console.data(json.dumps([{"name": entry["name"], "size": entry.get("size"),
                                  "type": entry.get("type", "file")} for entry in entries]))
        return 0
    if not entries:
        console.say(f"nothing under {console.value(path or '/')}")
        return 0
    console.table(["Path", "Size"], [[entry["name"], entry["size"]] for entry in entries],
                  ["bold"])
    return 0


def get(store, path, dest: Path) -> int:
    store.fs().get(path, str(dest))
    console.done(f"saved {console.value(path)} to {console.value(dest)}")
    return 0


def query(store, sql) -> int:
    import duckdb

    fs = store.fs()
    with tempfile.TemporaryDirectory(prefix="pdt-storage-") as tmp:
        rewritten = sql
        for pattern in set(re.findall(r"'([^']+)'", sql)):
            matches = fs.glob(pattern)
            if not matches:
                console.error(f"no objects match {console.value(repr(pattern))}")
                return 1
            for match in matches:
                local_path = Path(tmp) / match
                local_path.parent.mkdir(parents=True, exist_ok=True)
                fs.get(match, str(local_path))
            rewritten = rewritten.replace(f"'{pattern}'", f"'{Path(tmp) / pattern}'")
        result = duckdb.sql(rewritten)
        console.table([column[0] for column in result.description], result.fetchall())
    return 0


def unlock(store, app, assume_yes) -> int:
    from pdt import deploy

    held = store.read_lock()
    if held is None:
        console.say(f"no run holds the state of {console.value(app)}; nothing to unlock")
        return 0
    holder = f"run {held.get('run', '?')} started at {held.get('started', '?')}"
    if held.get("host"):
        holder += f" on {held['host']}"
    if not deploy.confirm([f"release the state lock held by {holder}",
                           "only do this when that run is no longer running"], assume_yes):
        return 1
    store.unlock()
    console.done(f"released the state lock of {console.value(app)}")
    return 0


def destroy(store, app, assume_yes) -> int:
    from pdt import deploy

    fs = store.fs()
    objects = fs.find("")
    if not objects:
        console.say(f"no objects under {console.value(f'{app}/')}")
        return 0
    if not deploy.confirm([console.Markup(
            f"delete {len(objects)} object(s) under {console.value(f'{app}/')}")], assume_yes):
        return 1
    fs.rm(objects)
    console.done(f"deleted {len(objects)} object(s) under {console.value(f'{app}/')}")
    return 0


def run(store, app: dict, rest: list[str], assume_yes: bool) -> int:
    name = app["name"]
    if not app["storage"]:
        console.error(f"storage is turned off for {console.value(name)}; "
                      f"remove {console.value('storage: false')} from "
                      f"{console.value(f'{name}/config.yml')}")
        return 1
    as_json = "--json" in rest
    recursive = "--recursive" in rest
    assume_yes = assume_yes or "--yes" in rest
    rest = [arg for arg in rest if arg not in ("--json", "--recursive", "--yes")]
    handlers = {
        "ls": (0, lambda args: ls(store, args[0] if args else "", as_json, recursive)),
        "get": (1, lambda args: get(store, args[0],
                                    Path(args[1] if len(args) > 1 else Path(args[0]).name))),
        "query": (1, lambda args: query(store, args[0])),
        "unlock": (0, lambda args: unlock(store, name, assume_yes)),
        "destroy": (0, lambda args: destroy(store, name, assume_yes)),
    }
    subcommand, *args = rest or [""]
    if subcommand not in handlers:
        console.error(console.escape(USAGE))
        return 1
    if as_json and subcommand != "ls":
        console.error("--json works only with ls")
        return 1
    needed, handler = handlers[subcommand]
    if len(args) < needed:
        console.error(console.escape(USAGE))
        return 1
    return handler(args)
