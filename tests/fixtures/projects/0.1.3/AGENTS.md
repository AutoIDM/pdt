# AGENTS.md

This folder is a pdt project: a set of small scheduled jobs. Every folder holding a `run.py` and a `config.yml` is one app. `pdt.yml` holds the settings shared by every app.

## Working here

- Start a new app with `pdt new <name> --from <example>`; `pdt examples` lists the starting points. Do not copy an app folder by hand.
- An app declares its dependencies in the script header at the top of its `run.py`. The pinned `pdt-cli` version is the version a deployed job keeps running, so leave it alone unless the app is being redeployed.
- List the env vars an app reads under `env:` in its `config.yml`. Their values go in `.env`, which is never committed; `pdt deploy` uploads the ones that are set as cloud secrets.
- Check work with `pdt validate`, try it with `pdt run <name>`, ship it with `pdt deploy <name>`.
- Check a deployed app with `pdt health`, list its runs with `pdt runs <name> [--count 5] [--since 3d] [--span 1d]`, and read one run's log with `pdt logs <name> [N] [--count 5] [--since 3d] [--span 1d] --failed --errors` (the last 20 lines; `--lines 50` for more, `--head` for the first lines, `--full` for all); add `--json` to any of them for machine-readable output.
- Log with `log()` from `pdt.utils.log`; a plain `print()` also reaches the run's cloud logs, but without a severity.
- An app folder holding a `Dockerfile` is built from that file instead of the generated one when it deploys to a cloud provider. The build context is the app folder under its own name next to `pdt.yml`; a `.dockerignore` in the app folder, with patterns relative to it, keeps files out of the image.
