# AGENTS.md

This folder is a pdt project: a set of small scheduled jobs. Every folder holding a `config.yml` and a `run.py` is one Python app. Every folder holding a `config.yml` and one or more `.ps1` files but no `run.py` is one PowerShell app. `pdt.yml` holds the settings shared by every app.

## Working here

- Start a new app with `pdt new <name> --from <example>`; `pdt examples` lists the starting points. Do not copy an app folder by hand.
- An app declares its dependencies in the script header at the top of its `run.py`. The pinned `pdt-cli` version is the version a deployed job keeps running, so leave it alone unless the app is being redeployed.
- List the env vars an app reads under `env:` in its `config.yml`. Their values go in `.env`, which is never committed; `pdt deploy` uploads the ones that are set as cloud secrets. Each `$env:NAME` a PowerShell app's scripts read is required even when `config.yml` does not list it; list it under `env: optional:` when the scripts work without it. The same holds for each `os.environ["NAME"]` a Python app's `.py` files read; `os.environ.get("NAME")` and `os.getenv("NAME")` make it optional. pdt does not find a name that the code builds at run time.
- Check work with `pdt validate`, try it with `pdt run <name>`, ship it with `pdt deploy <name>`.
- Check a deployed app with `pdt health`, list its runs with `pdt runs <name> [--count 5] [--since 3d] [--span 1d]`, and read one run's log with `pdt logs <name> [N] [--count 5] [--since 3d] [--span 1d] --failed --errors` (the last 20 lines; `--lines 50` for more, `--head` for the first lines, `--full` for all, `--follow` to wait for a running run's lines); add `--json` to any of them for machine-readable output.
- Log with `log()` from `pdt.utils.log`; a plain `print()` also reaches the run's cloud logs, but without a severity.
- A `run.ps1` file is the only entry script. Otherwise, `run_scripts` lists the entry scripts in order, or pdt runs each `.ps1` file that no other `.ps1` file loads in name order. Do not use `run.ps1` and `run_scripts` together.
- `pdt new <name> --from-scripts` writes `requirements.psd1` (the modules to install) and `run.py` (how the scripts run) into a PowerShell app's folder; once there, `requirements.psd1` replaces the modules pdt finds in the scripts, and `run.py` replaces how pdt runs them.
- An app folder holding a `Dockerfile` is built from that file instead of the generated one when it deploys to a cloud provider. The build context is the app folder under its own name next to `pdt.yml`; a `.dockerignore` in the app folder, with patterns relative to it, keeps files out of the image.
