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
exports an empty folder as PDT_OUTPUT_DIR, then runs each entry script in
order through pwsh. It stops at the first failure unless `continue_on_error`
is true. The entry scripts are
`run_scripts` from config when set, otherwise what `pdt.powershell` finds:
every .ps1 in name order except those another file runs or imports. After
the scripts, also after a failure, every file left in PDT_OUTPUT_DIR goes
to `runs/<run>/output/` in the app's data store, unless the app sets
`storage: false`. It exits with the failing script's code.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

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
        entries, _helpers = powershell.split_files(
            powershell.extract(app_dir)["files"], app["run_scripts"])
    except powershell.PowerShellError as exc:
        die(1, str(exc))
    code = 0
    with tempfile.TemporaryDirectory(prefix="pdt-output-") as output:
        env = {**os.environ, "PDT_OUTPUT_DIR": output}
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
            if result != 0 and not app["continue_on_error"]:
                break
        files = [file for file in Path(output).rglob("*") if file.is_file()]
        if app["storage"] and files:
            store = storage.store()
            folder = store.run_folder() + "output/"
            store.push(Path(output), folder)
            log("info", f"uploaded {len(files)} output file(s) to {store.url}{folder}")
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
