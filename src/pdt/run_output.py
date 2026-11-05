"""`python -m pdt.run_output APP RUN DIR`: copy the output of one run of a deployed app into DIR.

RUN is the run's number, as `pdt runs APP` numbers it. A run's output is the folder
`runs/<time>-<run id>/` in the app's store (pdt.utils.storage). This module reads the list of runs
and the store through `pdt runs` and `pdt storage`, so it signs in to each provider the same way
those commands do. The pdt service and the hosted runner run it for the `artifacts` operation,
and upload DIR.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from pdt import console


def pdt_json(*args: str) -> object:
    proc = subprocess.run([sys.executable, "-m", "pdt.cli", *args], stdin=subprocess.DEVNULL,
                          capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"pdt {' '.join(args)} failed: {proc.stderr.strip()[-500:]}")
    return json.loads(proc.stdout or "null")


def run_id(app: str, number: int) -> str:
    for run in pdt_json("runs", app, "--json") or []:
        if run.get("number") == number:
            return str(run["id"])
    raise RuntimeError(f"{app} has no run {number}.")


def run_folder(app: str, identity: str) -> str | None:
    for entry in pdt_json("storage", app, "ls", "runs", "--json") or []:
        name = str(entry.get("name") or "").rstrip("/")
        if entry.get("type") == "directory" and name.endswith(f"-{identity}"):
            return name
    return None


def copy(app: str, number: int, target: Path) -> int:
    return copy_run(app, run_id(app, number), target)


def copy_run(app: str, identity: str, target: Path) -> int:
    folder = run_folder(app, identity)
    if folder is None:
        return 0
    count = 0
    for entry in pdt_json("storage", app, "ls", folder, "--recursive", "--json") or []:
        if entry.get("type") == "directory":
            continue
        name = str(entry["name"])
        relative = Path(name).relative_to(folder) if name.startswith(folder + "/") else Path(
            Path(name).name)
        if ".." in relative.parts:
            continue
        destination = target / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        proc = subprocess.run([sys.executable, "-m", "pdt.cli", "storage", app, "get", name,
                               str(destination)], stdin=subprocess.DEVNULL, check=False)
        if proc.returncode != 0:
            raise RuntimeError(f"pdt storage {app} get {name} failed")
        count += 1
    return count


def main(argv: list[str]) -> int:
    if len(argv) != 3 or not argv[1].isdigit():
        console.error("usage: python -m pdt.run_output APP RUN DIR")
        return 2
    app, number, target = argv[0], int(argv[1]), Path(argv[2])
    target.mkdir(parents=True, exist_ok=True)
    try:
        count = copy(app, number, target)
    except (RuntimeError, ValueError, KeyError) as e:
        console.error(console.escape(str(e)))
        return 1
    if count == 0:
        console.say(f"Run {console.value(number)} of {console.value(app)} left no output files.")
    else:
        console.done(f"Copied {console.value(count)} output files of run {console.value(number)} "
                     f"of {console.value(app)}.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
