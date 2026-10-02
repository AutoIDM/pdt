# Guide

How a pdt project is laid out, how apps keep files and secrets, and how to write your own app. Back to the [README](../README.md).

## Install

```
uv tool install pdt-cli
```

That puts a `pdt` command on your PATH. To update it later, run `uv tool upgrade pdt-cli`.

On Windows you can run `winget install AutoIDM.pdt` instead, which also installs `uv` if you do not have it and puts the same `pdt` command on your PATH. `winget upgrade AutoIDM.pdt` moves pdt to the latest release, and the first `pdt` command after an install or upgrade finishes setting up that version.

You can also clone this repository and run `./pdt` (or `.\pdt.bat` on Windows) instead. It installs `uv` for you if you do not have it. Both ways give you the same commands.

## Set up a project

A project is a folder holding `pdt.yml`. Each app is a folder inside it that contains a `run.py`. Every command except `init` finds the project by looking in the current folder, then each folder above it.

```
pdt init my-jobs
```

`pdt init` asks where the project should live, which cloud you want, and which region. It warns you if you are about to create a project somewhere unwise, such as your home folder. The folder name is optional: `pdt init` with no name uses the current folder, and `pdt init DIR` uses `DIR`, creating it if it is not there yet. Add `--yes` to take the defaults and answer nothing.

Region is the only setting it asks for. Your AWS account, Azure subscription, and Google Cloud project all come from your credentials the first time you deploy, and pdt writes the answer into `pdt.yml` so every later deploy checks against it.

Starting in an empty folder also gives you a working app called `hello-world`. Run it straight away:

```
cd my-jobs
pdt run hello-world
```

It writes one log line and nothing else. Edit `hello-world/run.py` to make it do real work, or delete the folder if you would rather start from one of the examples below. A folder that already holds your own files gets no starter app.

## Add an app

```
pdt examples
pdt new my-report --from impossible-travel-report
```

`pdt new` copies one of the examples bundled with pdt into your project. Open `my-report/env.template`, copy the names you need into `.env`, then:

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

## Deploy

```
pdt deploy my-report
```

The command prints a resource plan and a monthly cost estimate before it changes anything. Add `--yes` to skip the question. To remove everything it created:

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

Some apps need files from the last run: a Meltano bookmark, a database. Keep those in one local folder, and copy it down before the work and up after:

```python
from pathlib import Path
from pdt.utils import storage

store = storage.store()
lease = store.pull("state/", Path(".pdt-state"))
# do the work; keep anything the next run needs inside .pdt-state/
store.push(Path(".pdt-state"), "state/", lease)
```

`pull` locks the folder, so a second copy of your app cannot run at the same time and mix up the files. `push` checks that nobody else changed them, saves them, and unlocks. If a run crashes, the next one takes over the lock after 30 minutes.

From your own computer, `pdt storage APP ls`, `get`, and `query` read the files with your own cloud sign-in. `pdt storage APP destroy` is the only command that deletes them, and it asks first. An app that needs none of this sets `storage: false` in its `config.yml`.

An app that is not ready sets `enabled: false` in its `config.yml`. `pdt list` still shows it, and every other command acts as if the app is not there. `uv run run.py` in the app folder still runs it.

## Running pdt from CI

Every command reads its values from the environment it runs in. A build server sets them as CI variables, and no `.env` file is needed.

- A `.env` file is a convenience for a person working on their own machine. A value that is already in the environment wins over the same name in a `.env` file.
- `PDT_ENV_JSON` holds every value as one JSON object, for a CI system that keeps one secret instead of many. For example, `PDT_ENV_JSON={"PDT_TOKEN": "abc", "PDT_SMTP_USER": "reports@example.com"}`.
- pdt creates no `.env` file on a build server. It looks for the `CI` variable that build systems set, and for a missing terminal. Email authorization must therefore be done first: run `pdt run APP` once on a machine with a browser, then copy `PDT_SMTP_OAUTH_CACHE_B64` (or `PDT_GRAPH_MAIL_CACHE_B64` for Microsoft Graph) from your `.env` into the CI variables.
- `pdt deploy APP` and `pdt destroy APP` ask before they change anything. Add `--yes` so they proceed without asking.

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

An app is a folder with a `run.py` that has a `main()` function. It declares its own dependencies in a script header, and pdt is one of them:

```python
# /// script
# requires-python = ">=3.12"
# dependencies = ["pdt-cli[apps]==0.1.3"]
# ///
from pathlib import Path

from pdt.config import check_env, load_env, merged_app
from pdt.utils.log import log


def main() -> int:
    app = merged_app(Path(__file__).resolve().parent.name)
    load_env(app["dir"])
    ...
```

The pinned version matters. A deployed job keeps using the version in its header, so upgrading pdt on your machine does not change a job already running in the cloud.
