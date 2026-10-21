#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "pyyaml",
#     "python-dotenv",
#     "rich",
#     "backoff",
#     "fsspec",
# ]
# ///
"""Run a PowerShell app: the part run.py plays for a Python app.

usage: run_powershell.py <app dir>

The app's working folder must be <app dir>. `pdt run`, the Windows task
runner, and the container image start it there; it also runs as
`python -m pdt.run_powershell .` from an installed pdt-cli[apps].

It loads the app's config and env (PDT_ENV_JSON secrets become env vars),
stops when an env var that config.yml lists as required, or that a script
reads and config.yml does not list, is not set,
exports an empty folder as PDT_OUTPUT_DIR and the app's merged `config:`
section as JSON in PDT_CONFIG_JSON, then runs each entry script in
order through pwsh. It stops at the first failure unless `continue_on_error`
is true. The entry scripts are
`run_scripts` from config when set, otherwise what `pdt.powershell` finds:
every .ps1 in name order except those another file runs or imports. After
the scripts, also after a failure, every file left in PDT_OUTPUT_DIR goes
to `runs/<run>/output/` in the app's data store, unless the app sets
`storage: false`. It exits with the failing script's code.

PDT_STATE_DIR is a folder that outlives the run. When a script reads
`$env:PDT_STATE_DIR`, the files of `state/` in the app's data store are
downloaded into it under the state lock before the first script, and the
folder goes back to `state/` when every script succeeds. After a failure
`state/` stays as it was, so the next run starts from the last good state.
`storage.sync` gives a Python app the same folder. With `storage: false`,
or when no script reads it, PDT_STATE_DIR starts empty and is not kept.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Callable

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from pdt import config, powershell
from pdt.pwsh import ensure_pwsh
from pdt.utils import storage
from pdt.utils.log import die, log

USAGE = "usage: run_powershell.py <app dir>"


def pwsh_command(script: Path) -> str:
    """Fail on any error the script does not silence itself, and keep the script's own
    `exit N`. A bare `& script; exit $LASTEXITCODE` exits 0 after a `throw`."""
    return ("function Clear-Host {}; Set-Alias -Name cls -Value Clear-Host -Force; "
            "Set-Alias -Name clear -Value Clear-Host -Force; "
            "$ErrorActionPreference='Stop'; $PSNativeCommandUseErrorActionPreference=$true; "
            f"try {{ & {powershell.quoted(str(script))} }} catch {{ "
            "[Console]::Error.WriteLine(($_ | Out-String).TrimEnd()); "
            "[Console]::Error.WriteLine($_.ScriptStackTrace); exit 1 }; exit $LASTEXITCODE")


def reads_state(files: list[dict]) -> bool:
    """Whether a scanned script reads $env:PDT_STATE_DIR, so the run needs state/."""
    return any(read["name"].upper() == "PDT_STATE_DIR" for f in files for read in f["envReads"])


def run_entries(pwsh: str, app_dir: Path, entries: list[str], continue_on_error: bool,
                env: dict[str, str]) -> int:
    """Run each entry script through pwsh; the first failing script's exit code, else 0."""
    code = 0
    for script in entries:
        log("info", f"starting {script}")
        result = subprocess.run(
            [pwsh, "-NoProfile", "-NonInteractive", "-Command", pwsh_command(app_dir / script)],
            cwd=app_dir, env=env, stdin=subprocess.DEVNULL).returncode
        log("info", f"{script} ended", exit_code=result)
        if result != 0:
            log("error", f"{script} failed", exit_code=result)
            if code == 0:
                code = result
        if result != 0 and not continue_on_error:
            break
    return code


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        sys.stderr.write(USAGE + "\n")
        return 2
    app_dir = Path(argv[0]).resolve()
    try:
        app = config.merged_app(app_dir.name)
        config.load_env(app_dir)
    except config.ConfigError as exc:
        die(1, "config is not valid", problem=str(exc))
    missing = config.missing_env(app)
    if missing != "":
        die(1, f"env vars missing: {missing}")
    pwsh = ensure_pwsh()
    try:
        files = powershell.extract(app_dir)["files"]
        entries, _helpers = powershell.split_files(files, app["run_scripts"])
    except powershell.PowerShellError as exc:
        die(1, str(exc))
    return run_with_storage(app, reads_state(files), lambda output, state: run_entries(
        pwsh, app_dir, entries, app["continue_on_error"],
        {**os.environ, "PDT_OUTPUT_DIR": str(output), "PDT_STATE_DIR": str(state),
         "PDT_CONFIG_JSON": json.dumps(app["config"], default=str)}))


def run_with_storage(app: dict, keeps_state: bool, run: Callable[[Path, Path], int]) -> int:
    """`run(output, state)` with two new folders, then keep what the run left in them.

    Every file in `output`, also after a failure, goes to `runs/<run>/output/` in the app's
    data store. When `keeps_state`, `state/` is pulled into `state` under the state lock
    first and pushed back when the run exits 0. `pdt.run_docker` runs a Docker app the same
    way. With `storage: false` both folders start empty and nothing is kept.
    """
    store = storage.store() if app["storage"] else None
    with tempfile.TemporaryDirectory(prefix="pdt-run-") as local:
        output, state = Path(local) / "output", Path(local) / "state"
        output.mkdir()
        state.mkdir()
        lease = None
        if store is not None and keeps_state:
            try:
                lease = store.pull("state/", state)
            except storage.StorageLocked as exc:
                die(1, str(exc))
        try:
            code = run(output, state)
            kept = [file for file in output.rglob("*") if file.is_file()]
            if store is not None and kept:
                folder = store.run_folder() + "output/"
                store.push(output, folder)
                log("info", f"uploaded {len(kept)} output file(s) to {store.url}{folder}")
            if lease is not None and code == 0:
                store.push(state, "state/", lease)
                lease = None
                log("info", f"saved state to {store.url}state/")
            elif lease is not None:
                log("info", "the run failed, so state/ keeps the last good run's files")
        finally:
            if lease is not None:
                store.abort(lease)
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
