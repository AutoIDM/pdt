# Using pdt with AI agents

This guide is for AI coding agents and LLM workflows interacting with a pdt project. (When you run `pdt init`, pdt also writes a compact `AGENTS.md` and `CLAUDE.md` directly into your project folder. Note that the `AGENTS.md` at the root of the `AutoIDM/pdt` source repository is for contributors developing `pdt-cli`, not for user projects.)

## Safe agent workflow

Follow this loop whenever creating, modifying, or deploying an app:

1. **Discover the project:** Run `pdt list --json` to see every app, its `profile` (`job` or `web`), `schedule`, `platform`, `enabled`, `paused`, and any config `error`.
2. **Scaffold instead of copying folders by hand:** Run `pdt examples`, then `pdt new APP --from EXAMPLE` (or `pdt new APP --from-scripts` to generate `requirements.psd1` and `run.py` for an existing PowerShell app).
3. **Validate before running:** Run `pdt validate --json` to catch YAML key mistakes, missing env vars, PowerShell module problems, and email config issues with zero cloud calls.
4. **Test locally first:** Run `pdt run APP` to verify behavior on the local machine with local storage (`.pdt/storage/APP/`).
5. **Always preview deploys before applying:** Run `pdt deploy APP --plan` and present the resource plan and monthly cost estimate to the human before deploying. Only pass `pdt deploy APP --yes` (or `--yes --run` for a job) after explicit user approval.
6. **Verify after deploy:** Check `pdt health --json`, `pdt runs APP --json`, and `pdt logs APP --failed --json` (or `pdt logs APP --json`).

## Machine-readable output (`--json`) and exit codes

Pass `--json` to keep stdout strictly valid JSON (all prompts, status lines, and errors move to stderr). Commands that fail to produce JSON exit non-zero with empty stdout.

| Command | JSON shape on stdout |
| --- | --- |
| `pdt list --json` | `[{"name", "profile", "schedule", "platform", "enabled", "paused", "error"}]` |
| `pdt validate --json` | `{"ok": bool, "problems": [{"app": str \| null, "message": str}]}` |
| `pdt health [APP] [--all] --json` | List of app health objects (exits `1` when any checked app failed) |
| `pdt runs APP [--count N] [--since ...] [--span ...] --json` | List of run records (newest first; `[]` if project has no enabled apps) |
| `pdt logs APP [N] [--failed] [--id ID] [--errors] [--full] --json` | Log payload for the selected run (or object keyed by run ID when `--id` is repeated) |
| `pdt storage APP ls [PATH] [--recursive] --json` | `[{"name", "files", "size", "type"}]` |

Exit codes: `0` success, `1` operational/validation/health error, `2` CLI usage error, `130` interrupted.

## Key rules and common pitfalls

- **Never read or print secret values from `.env`:** Check which keys are missing using `pdt validate --json` or `pdt secrets APP diff`. To update a single deployed secret without exposing it in command history, pipe its value on stdin to `pdt secrets APP set NAME`.
- **Know how env var auto-scanning works:**
  - In Python, `os.environ["NAME"]` automatically marks `NAME` as **required**; `os.environ.get("NAME")` and `os.getenv("NAME")` mark it **optional**.
  - In PowerShell, any `$env:NAME` read (unless written in the scripts) is automatically **required**; list it under `env: optional:` in the app's `pdt.yml` if the scripts work without it.
  - Names starting with `PDT_` are **skipped** by the scanner—always list any `PDT_*` env vars your app needs under `env:` in `pdt.yml`, or `pdt deploy` will not upload them.
- **Respect app profiles:**
  - `app_profile: job` (default) runs on a `schedule:` (under `job:`) and exits. Supports `aws`, `azure`, `google-cloud`, and `windows`.
  - `app_profile: web` serves HTTP on `0.0.0.0` at `PDT_PORT`, must NOT set `schedule:`, requires `web.auth` (`provider: google` with `allowed_domains` and/or `allowed_groups`), and deploys only to `google-cloud`.
- **Never edit `.pdt/state`:** pdt manages `.pdt/state` to know where each app is deployed. To move an app to another provider or region, destroy it first (`pdt destroy APP`) or run `pdt init` in the project root.
- **Keep `pdt.yml` clean:** App-level settings go in `<app>/pdt.yml` (the deprecated `config.yml` name should be renamed to `pdt.yml`). Never set `name:` inside an example or unless it matches the app folder name.
- **No pdt server:** Standalone pdt needs no external server—everything runs locally or inside the user's own cloud account.
