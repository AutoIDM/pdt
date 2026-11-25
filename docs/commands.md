# Commands

Inside an app folder, or any folder below it, leave `APP` off any command that takes one and pdt uses that app. For example, `pdt logs 3` in the `hello-world` folder reads run 3 of `hello-world`. Anywhere else, leave `APP` off or mistype it and pdt lists the apps in the project so you can pick one; a mistyped name also gets the closest app name. A mistyped command or option gets the closest one too, for example `pdt lgos` asks "Did you mean `pdt logs`?".

## Quick reference

| Command | What it does |
| --- | --- |
| `pdt init [DIR]` | Create a project here or in `DIR` (or change provider in an existing project) |
| `pdt examples` | List the bundled example apps |
| `pdt new APP [--from EXAMPLE \| --from-scripts]` | Add an app from an example, or scaffold `run.py` and `requirements.psd1` for PowerShell scripts |
| `pdt list [--names \| --json]` | Show every app with its profile, schedule, platform, enabled, and paused state |
| `pdt validate [--json]` | Check project/app config files, PowerShell modules, and required env vars |
| `pdt run APP [--deployed [--follow]]` | Run an app locally, or trigger/open a deployed app |
| `pdt deploy APP [--plan \| --yes \| --run \| --rebuild]` | Preview or deploy an app (`--all [--skip-failures]` for all enabled apps) |
| `pdt destroy APP [--yes]` | Remove all deployed resources for an app (`--all [--skip-failures]` for all deployed apps) |
| `pdt login APP` | Sign in again to the app's cloud provider |
| `pdt pause APP` / `pdt unpause APP` | Pause or resume a job's schedule, or stop/start a web app |
| `pdt health [APP \| --all] [--json]` | Check whether each deployed app's last run succeeded (exits 1 on failure) |
| `pdt runs APP [--since ...] [--span ...] [--count N] [--open \| --json]` | List a deployed app's recent runs and exit codes |
| `pdt logs APP [N] [--failed] [--id ID] [--errors] [--follow] [--json]` | Read or tail logs for a deployed app (or `--list` to list run logs) |
| `pdt alerts setup` / `pdt alerts test` | Configure or test Slack and Microsoft Teams notifications |
| `pdt gui [--port N] [--background] [--no-browser] [--stop]` | Open the local browser dashboard and DuckDB SQL workbench |
| `pdt secrets APP [diff \| save \| get \| set NAME] [--yes]` | Compare, push, fetch, or set a deployed app's secrets |
| `pdt storage APP ls\|get\|query\|unlock\|destroy` | Browse, download, query with SQL, unlock state, or delete an app's stored files |
| `pdt aws ...` / `pdt az ...` / `pdt gcloud ...` | Run the managed AWS, Azure, or Google Cloud CLI (installs if missing) |
| `pdt upgrade` | Upgrade `pdt-cli` and raise the `pdt-cli` pin in every app's `run.py` |
| `pdt completion [SHELL] [--script]` | Install or print shell tab-completion scripts |

## Get started

- **`pdt init [DIR] [--yes]`** — Create a project in `DIR` (or the current folder) with `pdt.yml`, `.env`, `.gitignore`, `AGENTS.md`, and `CLAUDE.md`. In an empty folder, also adds the `hello-world` starter app. Running `pdt init` in an existing project lets you switch providers and choose which deployed apps to move. `--yes` accepts defaults without asking.
- **`pdt examples`** — List every example app bundled with pdt and its summary.
- **`pdt new APP [--from EXAMPLE | --from-scripts]`** — Copy an example (default `hello-world`) into `APP/`. With `--from-scripts`, scans existing `.ps1` files in `APP/` and writes pinned `requirements.psd1` and `run.py` files so you can customize module versions or execution.
- **`pdt upgrade`** — Upgrade `pdt-cli` to the newest PyPI release (when installed via `uv tool`, the MSI, or winget) and raise the `pdt-cli` version pinned in each app's `run.py`. On Windows the upgrade finishes in a new window once the command exits. Deployed apps pick up the new version on their next `pdt deploy`.
- **`pdt completion [SHELL] [--script]`** — Enable tab completion for your shell (auto-detected when `SHELL` is omitted). Add `--script` to print the completion script instead of editing shell config files.

## Run and validate

- **`pdt list [--names | --json]`** — Show every app in the project with its profile, schedule, platform, `enabled`, and `paused` status. `--names` prints only enabled app names (one per line). `--json` prints a JSON array of objects with `name`, `profile`, `schedule`, `platform`, `enabled`, `paused`, and `error` (string or `null`).
- **`pdt validate [--json]`** — Validate `pdt.yml` files, scan PowerShell scripts and modules, verify required env vars, and check email config. Exits `1` if any problem is found. `--json` prints `{"ok": true|false, "problems": [{"app": "..."|null, "message": "..."}]}`.
- **`pdt run APP [--deployed [--follow]]`** — Run `APP` locally using the project's config, `.env`, and `.pdt/storage/APP/`. For a web app, serves locally on `http://127.0.0.1:<port>/` and opens the browser. With `--deployed`, starts one run of a deployed job immediately (or opens a deployed web app's URL). Add `--follow` with `--deployed` on a job to stream its log until completion and exit with the run's exit code (`0` on success, `1` on failure).

## Deploy

- **`pdt deploy APP [--yes] [--plan] [--run] [--rebuild]`** — Validate, print a resource plan and monthly cost estimate, ask to proceed, build the image (or configure Task Scheduler on Windows), sync secrets, and reconcile cloud resources.
  - `--yes` (`-y`) skips confirmation prompts and starts no post-deploy run.
  - `--run` starts one run of a deployed job after deploy and follows its log without asking.
  - `--rebuild` forces an image rebuild and push even when the app did not change.
  - `--plan` prints the plan and monthly cost estimate and exits `0` without changing any cloud resource, `pdt.yml`, or `.pdt/state`, signing in, or installing tools (except enabling free required Google Cloud APIs needed to price the project; local uv/price/scan caches may be written). Cannot be combined with `--all`, `--yes`, `--run`, or `--rebuild`.
- **`pdt deploy --all [--yes] [--skip-failures] [--run] [--rebuild]`** — Deploy every enabled app in order. Skips an app if you decline its plan; asks whether to skip and disable a failing app unless `--skip-failures` is set. With `--run`, runs and follows each deployed job one at a time after all deploys complete.
- **`pdt destroy APP [--yes]`** — Print every cloud or Windows resource pdt will remove for `APP`, ask to confirm, and delete them (cleaning up shared resources once no app uses them). Leaves stored data in place; use `pdt storage APP destroy` to delete stored files.
- **`pdt destroy --all [--yes] [--skip-failures]`** — Destroy every deployed app in order, confirming per app unless `--yes` is set.
- **`pdt login APP`** — Sign in again to the app's cloud provider, or switch cloud accounts.
- **`pdt pause APP` / `pdt unpause APP`** — Write `pause: true` or `pause: false` into the app's `pdt.yml` and apply it immediately: stops or resumes a job's deployed schedule (manual `pdt run APP --deployed` still works while paused), or stops/starts a web app's server.

## Check a deployed app

- **`pdt health [APP] [--all] [--json]`** — Show whether each deployed app's last run succeeded (or whether a web app is healthy); exits `1` when any checked app failed. Inside an app folder, checks only that app unless `--all` is passed.
- **`pdt runs APP [--since WINDOW] [--span DURATION] [--count N] [--open] [--json]`** — List recent runs of a deployed app with exit codes. Defaults to the 10 newest runs. `--since` accepts `12h`, `3d`, `2w`, `2026-09-20`, or `2026-09-20T14:00`; `--span` limits runs to a duration after `--since` (`12h`, `3d`, `2w`); `--count N` keeps at most `N` newest runs. Run numbers stay stable across filters. `--open` opens the runs view in Task Scheduler or the cloud console.
- **`pdt logs APP [N] [--id ID] [--failed] [--errors] [--since ...] [--span ...] [--count N] [--lines N] [--head] [--full] [--follow] [--list] [--open] [--json]`** — Read the log of run `N` as `pdt runs` numbers it (default `1`, the newest).
  - Selection: `--failed` picks the newest failed run; `--id ID` picks by run ID (repeatable; `--json` returns an object keyed by ID); `--list` lists available run logs with the same window flags as `pdt runs`.
  - Filtering & paging: `--errors` omits `DEBUG` and `INFO` lines; defaults to the last 20 lines (`--lines N`, `--head` for first lines, `--full` for all lines).
  - Live tailing: `--follow` streams lines in time order until the run finishes (using Azure container log stream, Cloud Logging tail, or CloudWatch Logs Live Tail while running).
  - Opening: `--open` opens the logs folder on Windows or the cloud console (for the app, or for a specific run when given `N`, `--failed`, or `--id`).
- **`pdt alerts setup` / `pdt alerts test`** — Interactively configure Slack or Microsoft Teams alert entries in `pdt.yml` and save webhook URLs in `.env`, or send a test notification to every configured alert. See [Alerts](alerts.md).
- **`pdt gui [--port N] [--background] [--no-browser] [--stop]`** — Start the local browser dashboard (`--background` leaves it running; `--stop` ends a background server). See [The dashboard](dashboard.md).

## Manage app data

- **`pdt secrets APP [diff | save | get | set NAME] [--yes]`** — Manage deployed secrets for `APP` (default action is `diff`):
  - `diff` shows which `.env` values differ from the deployed app without printing secret values.
  - `save` uploads your local `.env` values to the deployed app's cloud secret store.
  - `get` copies deployed values into a `.env.<provider>` file in the app folder (never read automatically by pdt).
  - `set NAME` reads a single value from stdin and updates `NAME` in the deployed app's secrets.
- **`pdt storage APP ls\|get\|query\|unlock\|destroy`** — Inspect and manage the app's persistent data store (see [Keeping files between runs](storage.md)):
  - `ls [PATH] [--recursive] [--json]` — List folders and files with counts and sizes.
  - `get PATH [DEST] [--open]` — Download a file or folder (with `--open`, opens the saved file; on Windows, Tab completes paths after `ls` and `get`).
  - `query SQL` — Run a DuckDB SQL query against stored CSV/Parquet/JSON files referenced in single quotes.
  - `unlock [--yes]` — Release a stale state lock left behind by a crashed run.
  - `destroy [--yes]` — Delete all stored files for `APP` (the only command that deletes storage data).
  - `--open` (with no subcommand) — Open the app's storage folder in File Explorer or the cloud console.

## Cloud CLIs

- **`pdt aws ...` / `pdt az ...` / `pdt gcloud ...`** — Forward arguments straight to the cloud provider CLI that pdt manages, installing it first if missing. For example, `pdt az account list`.

## JSON output and exit codes

With `--json`, `pdt list`, `pdt validate`, `pdt runs`, `pdt logs`, `pdt health`, and `pdt storage APP ls` print only JSON on stdout and send all other output (status lines, warnings, errors, sign-in prompts) to stderr. In a project with no enabled apps, `pdt runs`, `pdt logs`, `pdt health`, and `pdt storage APP ls` print `[]`. A command that has no JSON to print exits non-zero with stdout empty.

| Exit code | Meaning |
| --- | --- |
| `0` | Success (all checks valid, run/deploy succeeded, or all checked apps healthy) |
| `1` | Operational error, validation problem, failed run, declined confirmation, or unhealthy app |
| `2` | Invalid CLI arguments or unknown command/option |
| `130` | Interrupted by `Ctrl+C` |
