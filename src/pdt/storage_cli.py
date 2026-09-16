"""CLI helpers behind `pdt storage`, called by every provider's storage command.

Each function takes a `pdt.utils.storage.Store` already scoped to one app.
"""

from __future__ import annotations

import re
import tempfile
from pathlib import Path

from pdt import console

USAGE = "usage: pdt storage <app> ls [path] | get <path> [dest] | query <sql> | destroy [--yes]"


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


def destroy(store, app, assume_yes) -> int:
    from pdt import deploy

    fs = store.fs()
    try:
        objects = fs.find("")
    except FileNotFoundError:
        objects = []
    if not objects:
        console.say(f"no objects under {app}/")
        return 0
    if not deploy.confirm([f"delete {len(objects)} object(s) under {app}/"], assume_yes):
        return 1
    for path in objects:
        fs.rm(path)
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
