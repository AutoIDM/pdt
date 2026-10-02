# pdt

Run scheduled jobs — reports, integrations, automations — and deploy them to AWS, Azure, Google Cloud, or Windows Task Scheduler with one command.

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

## Commands

| Command | What it does |
| --- | --- |
| `pdt init [DIR]` | create a project here, or in DIR |
| `pdt examples` | list the example apps bundled with pdt |
| `pdt new APP --from EXAMPLE` | add an app to the project |
| `pdt list` | show every app with its schedule and platform; `--names` prints only the enabled app names |
| `pdt validate` | check the config files and the required env vars |
| `pdt run APP` | run an app on this machine |
| `pdt run APP --deployed` | start one run of the deployed job now, on its platform |
| `pdt deploy APP` | deploy an app to its configured platform |
| `pdt deploy --all` | deploy every enabled app, in order; asks whether to skip and disable an app that fails; add `--yes --skip-failures` to run unattended |
| `pdt destroy APP` | remove everything deploy created |
| `pdt pause APP` | stop the deployed schedule from starting runs; writes `pause: true` into the app's `config.yml` |
| `pdt unpause APP` | let the schedule start runs again; writes `pause: false` |
| `pdt secrets APP` | show which `.env` values differ from the deployed app |
| `pdt secrets APP save` | send your `.env` values to the deployed app; the next run uses them |
| `pdt secrets APP get` | copy the deployed values into a `.env.<provider>` file |
| `pdt secrets APP set NAME` | put one value, read from stdin, into the deployed app's secrets |
| `pdt login APP` | sign in again to the app's platform |
| `pdt storage APP ls|get|query|destroy` | look at, fetch, query, or delete the app's stored files; `ls PATH --recursive` lists every file under a folder |
| `pdt runs APP` | list the deployed app's runs with each run's exit code: the 10 newest, or with `--since 3d` (or `12h`, `2w`, `2026-09-20`, `2026-09-20T14:00`) every run since then, `--span 1d` keeping only the runs within that long after `--since` and `--count N` keeping only the N newest; a run's number is its place among every run pdt can still find, so it stays the same whichever runs print |
| `pdt logs APP [N]` | read the log of run N as `pdt runs` numbers it; with no N, the newest run, `--failed` picks the newest failed run instead, and `--id ID` picks a run by the id `pdt runs` shows (repeat it to read several runs; their `--json` is then an object keyed by id); `--since`, `--span`, and `--count` limit which runs those two choose from, `--errors` leaves out DEBUG and INFO lines; the last 20 lines print, `--lines N` prints N instead, `--head` prints the first lines instead of the last, and `--full` prints every line |
| `pdt health [APP]` | show whether each app's last run succeeded; exits 1 when one failed |
| `pdt gui` | open the project's dashboard in your browser: every app's health, each app's run history, and each run's log and files, with buttons for pause, unpause, and run now; `--background` keeps the server up after the command returns and `pdt gui --stop` ends it; `--port N` and `--no-browser` are there for the rare case |
| `pdt az ...` | run the Azure CLI that pdt installs |
| `pdt gcloud ...` | run the Google Cloud CLI that pdt installs |
| `pdt completion [SHELL]` | turn on tab completion for a shell |

Leave `APP` off `run`, `deploy`, `destroy`, `pause`, `unpause`, `secrets`, or `login`, or mistype it, and pdt lists the apps in the project so you can pick one.

`pdt az` and `pdt gcloud` hand your arguments straight to the cloud tool, and install it first if it is missing. For example, `pdt az account list`.

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

## The dashboard

`pdt gui` starts a small web server on your own computer and opens it in your browser. The first page is `pdt health` as a table, with a grid of every run by hour, day, week, or month underneath it. Click an app for its run history, and click a run for its log and the files it kept in storage. A CSV file a run kept opens in a SQL workbench in the browser: a table you can sort, filter, and page, and a SQL view where every CSV of the run is a view you can query and join, with the matching `pdt storage APP query` command shown under it. The workbench runs on DuckDB, which the first `pdt gui` downloads once into the pdt data folder (`~/.local/share/pdt`); a Download link next to each file still saves it. Every number on a page came out of a pdt command (`pdt runs`, `pdt logs`, `pdt storage ls`), and every button runs one (`pdt pause`, `pdt unpause`, `pdt run --deployed`); hover a button to see which. The pages keep what they fetched in `.pdt/gui.sqlite3` inside the project and fetch again when it is older than five minutes or when you press Refresh, so a run stays in the history after the cloud has forgotten it. A worker inside the server checks for work every two minutes: it refreshes each app's runs, asks the provider once for every run it still keeps, and then fetches the log and files of each run that lacks them, newest first, until every run has them. A page shows what is there; it only waits when it asks for something the worker has not reached yet. The Stats link shows how long each page and each pdt command took. The server listens on localhost only and has no login. `pdt gui --background` leaves it running after the command returns; `pdt gui --stop` ends it.

An app that should stay deployed but not run for a while sets `pause: true`. The schedule stays in place and does not fire until the key goes back to `false`. `pdt pause APP` and `pdt unpause APP` write the key into the app's `config.yml` and apply it to the deployed schedule in the same step; `pdt deploy` also honors it. A paused app still runs when you ask for it with `pdt run APP --deployed`.

## Running pdt from CI

Every command reads its values from the environment it runs in. A build server sets them as CI variables, and no `.env` file is needed.

- A `.env` file is a convenience for a person working on their own machine. A value that is already in the environment wins over the same name in a `.env` file.
- `PDT_ENV_JSON` holds every value as one JSON object, for a CI system that keeps one secret instead of many. For example, `PDT_ENV_JSON={"PDT_TOKEN": "abc", "PDT_SMTP_USER": "reports@example.com"}`.
- pdt creates no `.env` file on a build server. It looks for the `CI` variable that build systems set, and for a missing terminal. Email authorization must therefore be done first: run `pdt run APP` once on a machine with a browser, then copy `PDT_SMTP_OAUTH_CACHE_B64` (or `PDT_GRAPH_MAIL_CACHE_B64` for Microsoft Graph) from your `.env` into the CI variables.
- `pdt deploy APP` and `pdt destroy APP` ask before they change anything. Add `--yes` so they proceed without asking.

## Choosing where jobs run

Set `platform:` in `pdt.yml` for every app, or in an app's own `config.yml` for one app. An app's own file wins. `timezone` may live under `platform:` as the default for every app, and an app's own `timezone` overrides it.

### Azure

```yaml
platform:
  provider: azure
  subscription: 00000000-0000-0000-0000-000000000000
  region: eastus
  resource_group: pdt
  # Optional. An environment you already own, as <resource-group>/<name>.
  # environment: my-group/my-environment
```

`subscription` is optional. When it is missing or wrong, the deploy asks you to choose one.

Every pdt project in a subscription runs its jobs in one shared Container Apps environment per region, `pdt-<region>` in the resource group `pdt-shared`, because a subscription allows only a few environments. Destroying the last app that uses the environment removes it, and removes `pdt-shared` once it holds no environment. Set `environment` to use an environment you already have; pdt then never creates, changes, or deletes it.

### AWS

```yaml
platform:
  provider: aws
  region: us-east-1
  # Written for you on the first deploy, from your credentials.
  account: "123456789012"
  # Optional. Which profile in ~/.aws to use. Written for you if pdt has to ask.
  profile: work
```

`profile` is optional. When it is missing, pdt uses `AWS_PROFILE`, or the only profile on your computer. When there are several and none is chosen, or the one in the file is not on this computer, the deploy lists them and asks you to choose, then writes your answer here.

### Google Cloud

```yaml
platform:
  provider: google-cloud
  region: us-central1
  project: my-starter-project
```

`project` is optional. When it is missing or wrong, the deploy lists your projects and asks you to choose one, then writes your answer here.

### Bringing your own Dockerfile

Every cloud provider runs a job as a container: AWS on Batch (Fargate), Azure on Container Apps Jobs, Google Cloud on Cloud Run Jobs. The deploy builds an image for each app from a generated Dockerfile. It copies the app folder and `pdt.yml` into `/workspace`, installs the script-header dependencies of `run.py` with `uv sync --script`, and runs `run.py` as the entrypoint.

Put a `Dockerfile` in the app folder to build the image your own way, for example to add system packages or to install a heavy tool at build time instead of on every run. The build context is the same as the generated one: the app folder under its own name, next to `pdt.yml`. Start from the generated file:

```dockerfile
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim
COPY . /workspace
WORKDIR /workspace/my-app
ENV PDT_PROJECT=/workspace NO_COLOR=1 DBT_USE_COLORS=false
RUN uv sync --script run.py
ENTRYPOINT ["uv", "run", "--script", "run.py"]
```

A `.dockerignore` in the app folder keeps files out of the image. Write its patterns relative to the app folder (`.meltano`, `output`, `*.csv`); pdt moves them to the root of the build context for you. `.env` and the other secret files never reach the context, with or without a `.dockerignore`.

The Windows provider never builds an image, so `pdt validate` reports a Dockerfile in an app that uses it.

### Windows Task Scheduler

```yaml
platform:
  provider: windows

apps:
  - name: my-report
    schedule: daily
```

The Windows provider runs each job on the PC's local time, so `timezone` defaults to `local` there, the only value it accepts.

The job runs as the SYSTEM account. Windows accepts these schedules:

- `hourly`, `daily`, `weekly`, `monthly`, and `yearly`
- `*/N * * * *` minute intervals where `N` divides 60
- one fixed time daily
- one fixed time on selected weekdays
- one fixed time on selected days of selected months

Other cron forms are rejected, because they do not translate to Windows Task Scheduler.

Each app keeps its run data in `%ProgramData%\pdt\<app>\`. The task writes one log file per run to the `logs\` folder in it, named by the run's UTC start time, and `pdt runs <app>` and `pdt logs <app>` read those files. The app's own files (see [Keeping files between runs](#keeping-files-between-runs)) live in the `storage\` folder next to it. Deploy creates the folder and gives the SYSTEM account full control and you modify rights, so `pdt storage <app> destroy` works without an administrator. `pdt destroy <app>` removes the task and the `logs\` folder and keeps `storage\`. A run is stopped after 30 minutes, the same limit as the cloud providers, and a run that is still going when the next one is due is left alone; the new start is skipped.

## The bundled examples

Run `pdt examples` to list them, then `pdt new <name> --from <example>` to copy one.

### hello-world

Small app that logs "Hello world." Shows how to write a config.yml and useful as an empty starting project so you can write your own. `pdt init` puts a copy of this in every new empty project.

### impossible-travel-report

Looks at recent Entra ID sign-ins. Sends an email when one person signs in from two far-apart places too quickly: Dallas an hour ago, Paris now.

Settings for the lookback window, the minimum distance, and the email addresses live in the app's `config.yml`. The env vars it needs are listed there too, and in its `env.template`.

### monday-orphaned-account-report

Reports active Monday users whose email address does not match the `userPrincipalName` of an active Entra ID user.

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

# Utilities

`pdt.utils` provides built-in support for common functionality:

- Send emails
- Logging

## Send Email

`pdt.utils.send_email` sends plain-text mail.

Supports:

- SMTP + password
- SMTP + OAuth for Microsoft or Google
- Microsoft Graph delegated Mail.Send
- SES (Amazon Simple Email Service)
- Resend

### Configuration

#### SMTP

Required environment variables:

- PDT_SMTP_HOST
- PDT_SMTP_USER

When using password auth:

- PDT_SMTP_PASSWORD

When using OAuth for Google or Microsoft:

- PDT_SMTP_OAUTH_CLIENT_ID

Optional:

- PDT_SMTP_AUTH = oauth2
- PDT_SMTP_PORT: Port 587 is the default and uses STARTTLS; port 465 uses implicit TLS.

Google also requires:

- PDT_SMTP_OAUTH_CLIENT_SECRET

Microsoft optionally accepts:

- PDT_SMTP_OAUTH_TENANT_ID: Defaults to `common`.

PDT infers OAuth when the host is `smtp.gmail.com`, `smtp.office365.com`, or `smtp-mail.outlook.com` and no password is set. If a provider revokes authorization, run or deploy the app from a terminal again.

##### Google OAuth

Create a Desktop app OAuth client in the [Google Auth Platform](https://console.cloud.google.com/auth/clients). Set its client ID and client secret in the variables above. The OAuth consent screen must include `https://mail.google.com/` because Google requires that scope for SMTP OAuth.

##### Microsoft OAuth

Create an app registration in the [Microsoft Entra admin center](https://entra.microsoft.com/). Add a Mobile and desktop application platform with `http://localhost` as a redirect URI. Add the delegated Office 365 Exchange Online permission `https://outlook.office.com/SMTP.Send` and allow public client flows.

Microsoft 365 must also have Authenticated SMTP enabled for the sending mailbox. Use `PDT_SMTP_OAUTH_TENANT_ID` for a tenant-specific registration, or leave it empty to use `common`.

#### Microsoft Graph

Required environment variables:

- PDT_GRAPH_MAIL_USER
- PDT_GRAPH_MAIL_CLIENT_ID

Optional:

- PDT_GRAPH_MAIL_TENANT_ID: Defaults to `common`.

PDT sends as `PDT_GRAPH_MAIL_USER`, so `email_from` in config.yml must match it. Graph sends the HTML part alone; the other transports send both parts. If a provider revokes authorization, run or deploy the app from a terminal again.

Create an app registration in the [Microsoft Entra admin center](https://entra.microsoft.com/). Add a Mobile and desktop application platform with `http://localhost` as a redirect URI. Add the Microsoft Graph delegated permission `Mail.Send` and allow public client flows. Do not add a client secret.

#### SES

Required environment variables:

- PDT_SES_REGION

Optional:

- PDT_SES_ACCESS_KEY_ID
- PDT_SES_SECRET_ACCESS_KEY

Leave the two SES key variables empty to use the standard AWS credential chain (for example, `AWS_PROFILE`, environment credentials, or a workload role).

#### Resend

Required environment variables:

- PDT_RESEND_API_KEY

## Logging

`pdt.utils.log` writes one log line per event:

```python
from pdt.utils.log import die, log

log("info", "processed", rows=42)
```

Levels are `debug`, `info`, `warning`, and `error`. Locally a line is human-readable text on stdout; with `LOG_FORMAT=json` it is one JSON object per line, which is also the default on Cloud Run.
