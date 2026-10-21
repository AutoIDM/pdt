# Writing your own app

An app is a folder with a `run.py` that has a `main()` function. It declares its own dependencies in a script header, and pdt is one of them:

```python
# /// script
# requires-python = ">=3.12"
# dependencies = ["pdt-cli[apps]==0.1.13"]
# ///
from pathlib import Path

from pdt.config import check_env, load_env, merged_app
from pdt.utils.log import log


def main() -> int:
    app = merged_app(Path(__file__).resolve().parent.name)
    load_env(app["dir"])
    ...
```

The pinned version is the pdt version the app runs with, on your computer and in a deployed job. Upgrading pdt on your computer does not change it. Keep the pin at the newest pdt release, and run `pdt deploy APP` after you raise it, so the deployed job uses it too.

## PowerShell apps

A folder of `.ps1` files with no `run.py` is a PowerShell app: pdt reads the scripts to find the modules to install, then runs the entry scripts in name order through pwsh. Before `pdt run` starts the scripts on your computer, pdt installs each of those modules that is missing or at another version into its own folder (`~/.local/share/pdt/powershell-modules`, or `%LOCALAPPDATA%\pdt\powershell-modules` on Windows), so a local run loads the same versions a deploy installs. Other PowerShell sessions do not see that folder, and deleting it removes the modules.

A script reads the app's `config:` section from `$env:PDT_CONFIG_JSON | ConvertFrom-Json`, and pdt keeps every file a script writes to `$env:PDT_OUTPUT_DIR` in the app's data store. For files a later run needs, such as the last result to compare against, use `$env:PDT_STATE_DIR`: when a script reads it, pdt fills it with the app's saved state before the first script and saves it again when every script succeeds. After a failure the saved state stays as the last good run left it. While a run holds the state, another run of the same app stops with an error; `pdt storage <app> unlock` frees state that a dead run still holds.

A `run.ps1` file is the only entry script. Otherwise, use `run_scripts` in `config.yml` to choose entries and set their order. Do not use `run.ps1` and `run_scripts` together. By default, pdt stops after the first failed script. Set `continue_on_error: true` to run every entry script. The run still fails with the first script's exit code.

To change either step, run `pdt new <app> --from-scripts`. It writes `requirements.psd1`, which lists the modules with pinned versions, and `run.py`, which runs the scripts. Once they exist, pdt installs exactly the modules in `requirements.psd1` (`pdt validate` warns about a module the scripts need that it leaves out), and `pdt run` and deploy run your `run.py`.

```
pdt new my-report --from-scripts
```

## Docker apps (any language)

A folder with a `Dockerfile` and neither a `run.py` nor a `.ps1` file is a Docker app. Write the program in any language — Node.js, Go, Java, .NET, Rust, a shell script — and a Dockerfile that builds it. The `Dockerfile` builds in the app's own folder, exactly as `docker build .` would, and each run starts what its `ENTRYPOINT` and `CMD` say. `pdt new my-job --from node-hello-world` gives you one to start from:

```dockerfile
FROM node:22-bookworm-slim
WORKDIR /app
COPY hello.js .
CMD ["node", "hello.js"]
```

A Docker app gets what every other app gets. The program reads its `config:` section as JSON from the env var `PDT_CONFIG_JSON`, and its secrets from env vars. pdt keeps each file the program writes to the folder in `PDT_OUTPUT_DIR` in the app's data store. When a file in the app folder names `PDT_STATE_DIR`, that folder holds the app's saved state at the start of each run, and pdt saves it again when the run succeeds. pdt cannot read the env vars a program in another language uses, so list each one under `env:` in the app's `config.yml`.

To do this, the deploy adds a few lines to the end of your Dockerfile. They install pdt in folders of its own under `/opt`, then start your command inside pdt, so the run's log shows pdt's start and end lines and `pdt runs` sees the exit code. Your program, its working folder, and its `USER` stay as you wrote them. The image needs a Linux base with `sh`, such as Debian, Ubuntu, or Alpine.

`pdt run <app>` builds the Dockerfile with Docker on your computer and runs the command in a container. That is the one command that needs Docker running on your computer, because the program exists only in the image. A deploy does not need it: without Docker, the cloud builds the image. A Docker app deploys to AWS, Azure, or Google Cloud. The `windows` provider runs jobs on the PC without containers, so it cannot run one.

## Logging

`pdt.utils.log` writes one log line per event:

```python
from pdt.utils.log import die, log

log("info", "processed", rows=42)
```

Levels are `debug`, `info`, `warning`, and `error`. Locally a line is human-readable text on stdout; with `LOG_FORMAT=json` it is one JSON object per line, which is also the default on Cloud Run.
