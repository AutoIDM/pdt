# Guide

How a pdt project is laid out, how apps keep files and secrets, and how to write your own app. Back to the [README](../README.md).

## Install

```
uv tool install pdt-cli
uv tool update-shell
```

`uv tool install` puts the `pdt` command in uv's tool folder. `uv tool update-shell` adds that folder to your PATH if it is not there yet. A terminal window that was already open does not see a PATH change, so open a new window before you type `pdt`. To update pdt later, run `uv tool upgrade pdt-cli`.

On Windows, paste these lines into PowerShell. They install `uv` and pdt, and they work in the window you type them in:

```powershell
winget install --id astral-sh.uv -e
$env:Path = [Environment]::GetEnvironmentVariable('Path','Machine') + ';' + [Environment]::GetEnvironmentVariable('Path','User')
uv tool install pdt-cli
uv tool update-shell
$env:Path = [Environment]::GetEnvironmentVariable('Path','Machine') + ';' + [Environment]::GetEnvironmentVariable('Path','User')
pdt --version
```

`winget` and `uv tool update-shell` change the PATH for new windows only. Each `$env:Path = ...` line reads the new PATH into the current window. Another PowerShell window that was open before the install does not find `uv` or `pdt` until you paste the same line into it or open a new window.

On Windows you can run `winget install AutoIDM.pdt` instead, which also installs `uv` if you do not have it and puts the same `pdt` command on your PATH. Open a new PowerShell window after it finishes. `winget upgrade AutoIDM.pdt` moves pdt to the latest release, and the first `pdt` command after an install or upgrade finishes setting up that version.

You can also clone this repository and run `./pdt` (or `.\pdt.bat` on Windows) instead. It installs `uv` for you if you do not have it. Both ways give you the same commands.

## Set up a project

A project is a folder holding `pdt.yml`. Each app is a folder inside it. A Python app folder contains a `run.py`. A PowerShell app folder contains one or more `.ps1` files and no `run.py` (see [PowerShell apps](#powershell-apps)). Every command except `init` finds the project by looking in the current folder, then each folder above it.

```
pdt init my-jobs
```

The folder name is optional. `pdt init` chooses the project folder in one of three ways:

- `pdt init DIR` uses `DIR` and does not ask where the project should live. pdt creates `DIR` if it does not exist.
- `pdt init` with no DIR shows the current folder and asks where the project should live. The choices are "Make a new folder inside this one", "Use this folder", and "Cancel". For a new folder, pdt asks for its name (the default is `pdt-jobs`).
- `pdt init --yes` with no DIR uses the current folder and asks nothing.

When pdt asks where the project should live, it also warns you if the current folder is an unwise place for a project: your home folder, the top of the disk, or a folder that belongs to the operating system. After a warning, the default choice is a new folder inside the current folder.

Next, `pdt init` asks where your jobs should run: Microsoft Azure, Amazon Web Services, Google Cloud, this Windows PC (Task Scheduler), or "Decide later". For Azure, AWS, and Google Cloud, it then asks for a region. The Windows choice and the "Decide later" choice ask no more questions. With "Decide later" or `--yes`, `pdt.yml` gets a `platform:` block in comments, and you fill it in before you deploy (see [Choosing where jobs run](providers.md)).

If the folder already holds `pdt.yml`, `pdt init` says that the folder is already a pdt project and changes nothing. `pdt init` also writes `.gitignore`, `.env`, `AGENTS.md`, and `CLAUDE.md` (which points to `AGENTS.md`). It does not replace one of these files when it already exists.

Region is the only cloud setting it asks for. Your AWS account, Azure subscription, and Google Cloud project all come from your credentials the first time you deploy, and pdt writes the answer into `pdt.yml` so every later deploy checks against it.

pdt suggests the region nearest to you from your computer's time zone. When the time zone names no place, pdt suggests the region in your cloud CLI settings. When those settings name no region, pdt suggests a fixed default. If `pdt.yml` has no region when you first deploy, the deploy suggests one the same way, asks you to confirm it or type another, and saves your answer. With `--yes` it takes the suggestion and tells you which region it used.

Starting in an empty folder also gives you a working app called `hello-world`. Run it straight away:

```
cd my-jobs
pdt run hello-world
```

It writes one log line and nothing else. Edit `hello-world/run.py` to make it do real work, or delete the folder if you would rather start from one of [the bundled examples](#the-bundled-examples). A folder that already holds your own files gets no starter app.

## Add an app

```
pdt examples
pdt new my-report --from impossible-travel-report
```

`pdt new` copies one of the examples bundled with pdt into your project. If you leave out `--from`, `pdt new` lists the examples and stops, so you can choose one. Open `my-report/env.template`, copy the names you need into `.env`, then:

```
pdt validate
pdt run my-report
```

A project ends up looking like this:

```
my-jobs/
  pdt.yml           settings shared by every app
  .env              secrets, never committed
  my-report/
    run.py          the job
    config.yml      schedule, settings, and the env vars it needs
    env.template
```

## The bundled examples

Run `pdt examples` to list them, then `pdt new <name> --from <example>` to copy one.

| Example | What it does |
| --- | --- |
| `hello-world` | Logs a greeting. It shows how to write a `config.yml`, and you can use it as an empty app to start your own. `pdt init` puts a copy in every new empty project. |
| `impossible-travel-report` | Reads recent Entra ID sign-ins. It sends an email when one person signs in from two far-apart places too quickly: Dallas an hour ago, Paris now. The lookback window, the minimum distance, and the email addresses are settings in the app's `config.yml`. The `config.yml` and the `env.template` list the env vars that it needs. |
| `azure-license-waste` | A PowerShell app. Finds the Microsoft 365 licenses that a tenant pays for and nobody uses: paid licenses on disabled accounts, on accounts with no sign-in for 90 days, and seats that nobody has. Each run keeps a `LicenseWaste-<date>.xlsx` workbook in the app's data store, with the monthly and yearly cost. Edit `license-prices.csv` to match your prices. |
| `monday-orphaned-account-report` | Reports active Monday users whose email address does not match the `userPrincipalName` of an active Entra ID user. |
| `powershell-report` | A PowerShell app. Its `report.ps1` writes a small CSV file with `Export-Csv` into `PDT_OUTPUT_DIR`, and pdt keeps the file in the app's data store. |

## Deploy

```
pdt deploy my-report
```

The command prints a resource plan and a monthly cost estimate before it changes anything. Add `--yes` to skip the question. Deploy creates what is missing and updates what changed, so you can run it again after a failure.

pdt names the resources of one app `pdt-<app>`, and tags every resource that it creates with `managed-by=pdt`. Destroy deletes only resources with that tag. When no other app uses a shared resource, destroy deletes that resource too. To remove everything it created:

```
pdt destroy my-report
```

To sign in again, or to switch to a different cloud account:

```
pdt login my-report
```

## Keeping files between runs

Your app runs, writes some files, and stops. Then the computer it ran on is thrown away, and the files go with it. So pdt gives every app a folder in your cloud account that stays. Deploy the app, destroy it, deploy it again: the folder and everything in it is still there.

pdt creates one bucket per cloud account, named `pdt-data-` plus a short code, and gives each app its own folder inside it. When your app runs in the cloud, `PDT_STORAGE_URL` points at that folder. A Windows scheduled task points it at `%ProgramData%\pdt\APP\storage\`. When you run `pdt run APP` on your own computer, it points at `.pdt/storage/APP/` inside your project instead. The app code is the same in every place.

Write and read files with `pdt.utils.storage`:

```python
from pdt.utils import storage

store = storage.store()
with store.open("report.csv", "wb") as f:
    f.write(b"name,count\n")
```

`store.fs()` gives you the full [fsspec](https://filesystem-spec.readthedocs.io/) filesystem, rooted at your app's folder. Output from one run goes under `store.run_folder()`, which names a new `runs/<time>-<id>/` folder each run.

Some apps need files from the last run: a Meltano bookmark, a database. Keep those in the `state` folder that `storage.sync()` hands you, and put anything a person may want to look at afterwards in its `output` folder:

```python
from pdt.utils import storage

with storage.sync() as run:
    # do the work; keep anything the next run needs inside run.state/
    (run.output / "report.csv").write_text("name,count\n")
```

`sync` copies `state/` down from your app's folder and locks it, so a second copy of your app cannot run at the same time and mix up the files. When the block ends, it uploads `run.output` to this run's `runs/` folder, checks that nobody else changed the state files, saves them, and unlocks. When the block raises, or the run is interrupted, it still uploads `run.output`, unlocks, and leaves `state/` as it was, so the next run starts from the last good state. An app whose state is safe to keep at any point, such as a Meltano bookmark or an OAuth token that rotates on every login, passes `storage.sync(push_state_on_error=True)` so a failed run keeps what it saved. Both local folders live under `.pdt/runs/` in the app folder and stay there for inspection.

The same steps by hand are `store.pull("state/", folder)`, which returns a lease, then `store.push(folder, "state/", lease)` on success or `store.abort(lease)` on failure.

A run that dies without reaching either call leaves the lock behind. The next run takes it over right away when it can tell the holder is gone: the holder ran on the same computer and its process has ended, or it carries the same `PDT_RUN_ID`. Otherwise it waits 30 minutes, or you release the lock yourself with `pdt storage APP unlock`, which asks first.

From your own computer, `pdt storage APP ls`, `get`, and `query` read the files with your own cloud sign-in. `pdt storage APP destroy` is the only command that deletes them, and it asks first. An app that needs none of this sets `storage: false` in its `config.yml`.

An app that is not ready sets `enabled: false` in its `config.yml`. `pdt list` still shows it, and every other command acts as if the app is not there. `uv run run.py` in the app folder still runs it.

## The dashboard

`pdt gui` starts a small web server on your own computer and opens it in your browser. The first page is `pdt health` as a table, with a grid of every run by hour, day, week, or month underneath it. Click an app for its run history, and click a run for its log and the files it kept in storage. A CSV file a run kept opens in a SQL workbench in the browser: a table you can sort, filter, and page, and a SQL view where every CSV of the run is a view you can query and join, with the matching `pdt storage APP query` command shown under it. The workbench runs on DuckDB, which the first `pdt gui` downloads once into the pdt data folder (`~/.local/share/pdt`); a Download link next to each file still saves it. Every number on a page came out of a pdt command (`pdt runs`, `pdt logs`, `pdt storage ls`), and every button runs one (`pdt pause`, `pdt unpause`, `pdt run --deployed`); hover a button to see which. The pages keep what they fetched in `.pdt/gui.sqlite3` inside the project and fetch again when it is older than five minutes or when you press Refresh, so a run stays in the history after the cloud has forgotten it. A worker inside the server checks for work every two minutes: it refreshes each app's runs, asks the provider once for every run it still keeps, and then fetches the log and files of each run that lacks them, newest first, until every run has them. A page shows what is there; it only waits when it asks for something the worker has not reached yet. The Stats link shows how long each page and each pdt command took. The server listens on localhost only and has no login. `pdt gui --background` leaves it running after the command returns; `pdt gui --stop` ends it.

An app that should stay deployed but not run for a while sets `pause: true`. The schedule stays in place and does not fire until the key goes back to `false`. `pdt pause APP` and `pdt unpause APP` write the key into the app's `config.yml` and apply it to the deployed schedule in the same step; `pdt deploy` also honors it. A paused app still runs when you ask for it with `pdt run APP --deployed`.

## Running pdt from CI

Every command reads its values from the environment it runs in. A build server sets them as CI variables, and no `.env` file is needed.

- A `.env` file is a convenience for a person working on their own machine. A value that is already in the environment wins over the same name in a `.env` file.
- `PDT_ENV_JSON` holds every value as one JSON object, for a CI system that keeps one secret instead of many. For example, `PDT_ENV_JSON={"PDT_TOKEN": "abc", "PDT_SMTP_USER": "reports@example.com"}`.
- pdt creates no `.env` file on a build server. It looks for the `CI` variable that build systems set, and for a missing terminal. Email authorization must therefore be done first: run `pdt run APP` once on a machine with a browser, then copy `PDT_SMTP_OAUTH_CACHE_B64` (or `PDT_GRAPH_MAIL_CACHE_B64` for Microsoft Graph) from your `.env` into the CI variables.
- `pdt deploy APP` and `pdt destroy APP` ask before they change anything. Add `--yes` so they proceed without asking.
- With `--json`, `pdt list`, `pdt validate`, `pdt runs`, `pdt logs`, `pdt health`, and `pdt storage APP ls` print only JSON on stdout, and every other line, such as an error or a sign-in question, on stderr. In a project with no enabled apps, `pdt runs`, `pdt logs`, `pdt health`, and `pdt storage APP ls` print `[]`. A command that has no JSON to print exits with a code other than 0 and prints nothing on stdout.

## Where things live

| Item | Where |
| --- | --- |
| the `pdt` command and its code | the tool's own environment, from `uv tool install` |
| the `pdt` launcher from winget | `%LOCALAPPDATA%\Microsoft\WinGet\Packages`, linked from `%LOCALAPPDATA%\Microsoft\WinGet\Links` |
| the example apps | inside the pdt package, copied out by `pdt new` |
| your apps, `pdt.yml`, `.env` | your project folder, under version control |
| the Google Cloud CLI pdt downloads | `~/.local/share/pdt/gcloud`, or `%LOCALAPPDATA%\pdt\gcloud` |
| a Windows scheduled task's run logs and files | `%ProgramData%\pdt\<app>\logs` and `%ProgramData%\pdt\<app>\storage` |
| cloud sign-in state | `~/.azure` and `~/.config/gcloud`, as usual |

Set `PDT_PROJECT` to name the project folder directly, instead of letting pdt search upward. Deployed jobs get it set for them.

## Writing your own app

A Python app is a folder with a `run.py` that has a `main()` function. It declares its own dependencies in a script header, and pdt is one of them:

```python
# /// script
# requires-python = ">=3.12"
# dependencies = ["pdt-cli[apps]==0.1.6"]
# ///
from pathlib import Path

from pdt.config import check_env, load_env, merged_app
from pdt.utils.log import log


def main() -> int:
    app = merged_app(Path(__file__).resolve().parent.name)
    load_env(app["dir"])
    ...
```

The pinned version is the pdt version that the app runs with. Keep it at the newest pdt release. After you raise it, run `pdt deploy <app>` so the deployed job uses the new version.

### Settings and env vars

Each key under `config:` in an app's `config.yml` reaches the app as `app["config"]`. An env var named `PDT_<APP>_<KEY>` replaces the value of one key. For example, `PDT_HELLO_WORLD_GREETING` replaces `greeting` in the `hello-world` app.

List the env vars that an app reads under `env:` in its `config.yml`:

```yaml
env:
  required: [PDT_AZURE_TENANT_ID, PDT_AZURE_CLIENT_ID]
  one_of:
    - [PDT_AZURE_CLIENT_SECRET]
    - [PDT_AZURE_PRIVATE_KEY_B64]
  optional: [PDT_AZURE_PRIVATE_KEY_PASSWORD]
```

`pdt validate` reports a missing `required` value, and `pdt run` and `pdt deploy` stop. For `one_of`, all the names in at least one group must have a value. `pdt deploy` sends the values that are set to the cloud as secrets.

pdt also reads the code to find env vars. In a Python app, each `os.environ["NAME"]` in a `.py` file is a required env var, and each `os.environ.get("NAME")` or `os.getenv("NAME")` is an optional one. In a PowerShell app, each `$env:NAME` that the scripts read and do not set is a required env var, unless you list it under `optional` or `one_of`. Neither scan counts a name that starts with `PDT_` or a system variable such as `PATH` or `TEMP`. pdt does not find a name that the code builds while it runs.

### PowerShell apps

A folder of `.ps1` files with no `run.py` is a PowerShell app: pdt reads the scripts to find the modules to install, then runs the entry scripts in name order through pwsh. Before `pdt run` starts the scripts on your computer, pdt installs each of those modules that is missing or at another version into its own folder (`~/.local/share/pdt/powershell-modules`, or `%LOCALAPPDATA%\pdt\powershell-modules` on Windows), so a local run loads the same versions a deploy installs. Other PowerShell sessions do not see that folder, and deleting it removes the modules. A `run.ps1` file is the only entry script. Otherwise, use `run_scripts` in `config.yml` to choose entries and set their order. Without `run.ps1` and `run_scripts`, the entry scripts are the `.ps1` files in the app folder that no other `.ps1` file loads. Do not use `run.ps1` and `run_scripts` together. By default, pdt stops after the first failed script. Set `continue_on_error: true` to run every entry script. The run still fails with the first script's exit code. To change either step, run `pdt new <app> --from-scripts`. It writes `requirements.psd1`, which lists the modules with pinned versions, and `run.py`, which runs the scripts. Once they exist, pdt installs exactly the modules in `requirements.psd1` (`pdt validate` warns about a module the scripts need that it leaves out), and `pdt run` and deploy run your `run.py`.

```
pdt new my-report --from-scripts
```

pdt makes an empty folder for each run and puts its path in `PDT_OUTPUT_DIR`. After the scripts end, pdt copies each file in that folder to the app's data store, under `runs/<run>/output/`. In the cloud, the modules install when deploy builds the image. A module that does not load makes the build fail, not the first run.

The [`powershell-report`](#the-bundled-examples) example is a PowerShell app to start from.
