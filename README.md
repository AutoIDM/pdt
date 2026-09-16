# pdt

pdt runs small scheduled apps: reports, integrations, automations. You write an app as one Python file. One command deploys it to AWS, Azure, Google Cloud, or Windows Task Scheduler.

## Install

```
uv tool install pdt-cli
```

That puts a `pdt` command on your PATH. To update it later, run `uv tool upgrade pdt-cli`.

On Windows you can run `winget install AutoIDM.pdt` instead, which also installs `uv` if you do not have it and puts the same `pdt` command on your PATH. `winget upgrade AutoIDM.pdt` moves pdt to the latest release, and the first `pdt` command after an install or upgrade finishes setting up that version.

You can also clone this repository and run `./pdt` (or `.\pdt.bat` on Windows). That installs `uv` for you if you do not have it. Both ways give you the same commands.

## Create a project

A project is a folder holding `pdt.yml`. Each app is a folder inside it that holds a `run.py`. Every command except `init` finds the project by looking in the current folder, then in each folder above it.

```
pdt init my-jobs
```

`pdt init` asks which cloud you want and which region. Region is the only cloud setting it asks for. Your AWS account, Azure subscription, or Google Cloud project comes from your sign-in the first time you deploy, and pdt writes it into `pdt.yml` so every later deploy checks against it.

The folder name is optional. `pdt init` with no name asks whether to use the current folder or make a new one inside it, and warns you when the current folder is a poor place for a project, such as your home folder. Add `--yes` to take the defaults and answer nothing.

A project started in an empty folder gets a working app called `hello-world`. Run it now:

```
cd my-jobs
pdt run hello-world
```

It writes one log line and stops. Edit `hello-world/run.py` to make it do real work, or delete the folder and start from an example. A folder that already holds your own files gets no starter app.

## Add an app

```
pdt examples
pdt new my-report --from impossible-travel-report
```

`pdt new` copies one of the examples bundled with pdt into your project. Open `my-report/env.template`, copy the names you need into `.env`, fill in the values, then:

```
pdt validate
pdt run my-report
```

A project looks like this:

```
my-jobs/
  pdt.yml           settings shared by every app
  .env              secrets, never committed
  my-report/
    run.py          the app
    config.yml      its schedule, settings, and the env vars it needs
    env.template
```

## Deploy

```
pdt deploy my-report
```

The command prints a plan of the resources it will create and a monthly cost estimate. Then it asks before it changes anything. Add `--yes` to skip the question.

Run `pdt deploy my-report` again after you change the app. It updates what changed and leaves the rest alone.

To remove everything the deploy created:

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
| `pdt list` | show every app with its schedule and provider; `--names` prints only the enabled app names |
| `pdt validate` | check the config files and the required env vars |
| `pdt run APP` | run an app on this computer |
| `pdt deploy APP` | deploy an app to its cloud |
| `pdt deploy --all` | deploy every enabled app, in order; asks whether to skip and disable an app that fails; add `--yes --skip-failures` to run unattended |
| `pdt destroy APP` | remove everything deploy created |
| `pdt secrets APP` | show which `.env` values differ from the deployed app |
| `pdt secrets APP save` | send your `.env` values to the deployed app; the next run uses them |
| `pdt secrets APP get` | copy the deployed values into a `.env.<provider>` file |
| `pdt secrets APP set NAME` | put one value, read from stdin, into the deployed app's secrets |
| `pdt login APP` | sign in again to the app's cloud |
| `pdt storage APP ls\|get\|query\|destroy` | list, fetch, query, or delete the app's files in the data store |
| `pdt runs APP` | list the deployed app's runs with each run's exit code: the 10 newest, or with `--since 3d` (or `12h`, `2w`, `2026-09-20`, `2026-09-20T14:00`) every run since then, `--span 1d` keeping only the runs within that long after `--since` and `--count N` keeping only the N newest; a run's number is its place among every run pdt can still find, so it stays the same whichever runs print |
| `pdt logs APP [N]` | read the log of run N as `pdt runs` numbers it; with no N, the newest run, and `--failed` picks the newest failed run instead; `--since`, `--span`, and `--count` limit which runs those two choose from, `--errors` leaves out DEBUG and INFO lines; the last 20 lines print, `--lines N` prints N instead, `--head` prints the first lines instead of the last, and `--full` prints every line |
| `pdt health [APP]` | show whether each app's last run succeeded; exits 1 when one failed |
| `pdt aws ...`, `pdt az ...`, `pdt gcloud ...` | run that cloud's own command line tool, which pdt installs |
| `pdt completion [SHELL]` | turn on tab completion for a shell |

Leave `APP` off `run`, `deploy`, `destroy`, `secrets`, or `login`, or mistype it, and pdt lists the apps in the project so you can pick one.

`pdt aws`, `pdt az`, and `pdt gcloud` pass your arguments straight to the cloud tool, and install it first if it is missing. For example, `pdt az account list`.

## Choose where apps run

Set `platform:` in `pdt.yml` for every app, or in an app's own `config.yml` for one app. The app's own file wins.

Every cloud runs an app as a container: AWS on Batch (Fargate), Azure on Container Apps Jobs, Google Cloud on Cloud Run Jobs. `schedule` in `config.yml` is `hourly`, `daily`, `weekly`, `monthly`, `yearly`, or a 5-field cron expression such as `0 * * * *`. `timezone` names the zone the schedule is read in; the default is UTC. It may also live under `platform:` as the default for every app, and an app's own `timezone` overrides it.

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

`subscription` is optional. When it is missing or wrong, the deploy asks you to choose one. `resource_group` defaults to `pdt`.

Every pdt project in a subscription runs its apps in one shared Container Apps environment per region, `pdt-<region>` in the resource group `pdt-shared`, because a subscription allows only a few environments. Destroying the last app that uses the environment removes it, and removes `pdt-shared` once it holds no environment. Set `environment` to use an environment you already have; pdt then never creates, changes, or deletes it.

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

### Windows Task Scheduler

```yaml
platform:
  provider: windows

apps:
  - name: my-report
    schedule: daily
```

The Windows provider runs each app on the PC's local time, so `timezone` defaults to `local` there, the only value it accepts.

The app runs on this computer as the SYSTEM account. Windows accepts these schedules:

- `hourly`, `daily`, `weekly`, `monthly`, and `yearly`
- `*/N * * * *` minute intervals where `N` divides 60
- one fixed time daily
- one fixed time on selected weekdays
- one fixed time on selected days of selected months

Other cron forms are rejected, because they do not translate to Windows Task Scheduler.

Each app keeps its run data in `%ProgramData%\pdt\<app>\`. The task writes one log file per run to the `logs\` folder in it, named by the run's UTC start time, and `pdt runs <app>` and `pdt logs <app>` read those files. The app's own files (see [Keep files between runs](#keep-files-between-runs)) live in the `storage\` folder next to it. Deploy creates the folder and gives the SYSTEM account full control and you modify rights, so `pdt storage <app> destroy` works without an administrator. `pdt destroy <app>` removes the task and the `logs\` folder and keeps `storage\`. A run is stopped after 30 minutes, the same limit as the cloud providers, and a run that is still going when the next one is due is left alone; the new start is skipped.

### Bring your own Dockerfile

The deploy builds a container image for each app from a generated Dockerfile. It copies the app folder and `pdt.yml` into `/workspace`, installs the dependencies named in the script header of `run.py`, and runs `run.py`.

Put a `Dockerfile` in the app folder to build the image your own way, for example to add system packages or to install a large tool at build time instead of on every run. The build context is the same as the generated one: the app folder under its own name, next to `pdt.yml`. Start from the generated file:

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

## Keep files between runs

Your app runs, writes some files, and stops. The computer it ran on is then thrown away, and the files go with it. So pdt gives every app a folder in a data store in your cloud account, and that folder stays. Deploy the app, destroy it, deploy it again: the folder and everything in it is still there.

pdt creates one data store per cloud account: an S3 bucket, an Azure storage account, or a Cloud Storage bucket, named `pdt-data-` plus a short code (`pdtdata` plus the code on Azure, which allows no dash). Each app gets its own folder inside it. When your app runs in the cloud, `PDT_STORAGE_URL` points at that folder. A Windows scheduled task points it at `%ProgramData%\pdt\APP\storage\`. When you run `pdt run APP` on your own computer, it points at `.pdt/storage/APP/` inside your project instead. The app code is the same in every place.

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

## Run pdt from CI

Every command reads its values from the environment it runs in. A build server sets them as CI variables, and no `.env` file is needed.

- A `.env` file is for a person working on their own computer. A value already in the environment wins over the same name in a `.env` file.
- `PDT_ENV_JSON` holds every value as one JSON object, for a CI system that keeps one secret instead of many. For example, `PDT_ENV_JSON={"PDT_TOKEN": "abc", "PDT_SMTP_USER": "reports@example.com"}`.
- pdt opens no browser on a build server. It looks for the `CI` variable that build systems set, and for a missing terminal. Authorize email first: run `pdt run APP` once on a computer with a browser, then copy `PDT_SMTP_OAUTH_CACHE_B64` (or `PDT_GRAPH_MAIL_CACHE_B64` for Microsoft Graph) from your `.env` into the CI variables.
- `pdt deploy APP` and `pdt destroy APP` ask before they change anything. Add `--yes` so they proceed without asking.

## Write your own app

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

The pinned version matters. A deployed app keeps using the version in its header, so upgrading pdt on your computer does not change an app already running in the cloud.

`config.yml` holds the app's schedule, anything you want under `config:` (it reaches the app as `app["config"]`), and the env vars it needs under `env:`. `pdt validate` checks the env vars against your `.env`, and `pdt deploy` uploads the ones that are set as cloud secrets. The bundled examples show the layout.

### Logging

`pdt.utils.log` writes one log line per event:

```python
from pdt.utils.log import die, log

log("info", "processed", rows=42)
```

Levels are `debug`, `info`, `warning`, and `error`. On your computer a line is readable text on stdout. With `LOG_FORMAT=json` it is one JSON object per line, which is also the default on Cloud Run.

### Sending email

`pdt.utils.send_email` sends mail through one of these:

- SMTP with a password
- SMTP with OAuth, for Microsoft or Google
- Microsoft Graph (delegated `Mail.Send`)
- Amazon SES
- Resend

Set the env vars for one of them. If a provider revokes an OAuth authorization, run or deploy the app from a terminal again.

#### SMTP

Required:

- `PDT_SMTP_HOST`
- `PDT_SMTP_USER`
- `PDT_SMTP_PASSWORD` for password sign-in, or `PDT_SMTP_OAUTH_CLIENT_ID` for OAuth

Optional:

- `PDT_SMTP_AUTH=oauth2` forces OAuth. pdt infers it when the host is `smtp.gmail.com`, `smtp.office365.com`, or `smtp-mail.outlook.com` and no password is set.
- `PDT_SMTP_PORT`: 587 is the default and uses STARTTLS; 465 uses implicit TLS.
- `PDT_SMTP_OAUTH_CLIENT_SECRET`: Google requires it.
- `PDT_SMTP_OAUTH_TENANT_ID`: Microsoft only. Defaults to `common`.

For Google, create a Desktop app OAuth client in the [Google Auth Platform](https://console.cloud.google.com/auth/clients). Set its client ID and client secret in the variables above. The OAuth consent screen must include `https://mail.google.com/`, because Google requires that scope for SMTP OAuth.

For Microsoft, create an app registration in the [Microsoft Entra admin center](https://entra.microsoft.com/). Add a Mobile and desktop application platform with `http://localhost` as a redirect URI. Add the delegated Office 365 Exchange Online permission `https://outlook.office.com/SMTP.Send` and allow public client flows. Microsoft 365 must also have Authenticated SMTP enabled for the sending mailbox.

#### Microsoft Graph

Required:

- `PDT_GRAPH_MAIL_USER`
- `PDT_GRAPH_MAIL_CLIENT_ID`

Optional:

- `PDT_GRAPH_MAIL_TENANT_ID`: Defaults to `common`.

pdt sends as `PDT_GRAPH_MAIL_USER`, so `email_from` in `config.yml` must match it. Graph sends the HTML part alone; the other transports send both the text and the HTML part.

Create an app registration in the [Microsoft Entra admin center](https://entra.microsoft.com/). Add a Mobile and desktop application platform with `http://localhost` as a redirect URI. Add the Microsoft Graph delegated permission `Mail.Send` and allow public client flows. Do not add a client secret.

#### Amazon SES

Required:

- `PDT_SES_REGION`

Optional:

- `PDT_SES_ACCESS_KEY_ID`
- `PDT_SES_SECRET_ACCESS_KEY`

Leave the two key variables empty to use the standard AWS credential chain (for example `AWS_PROFILE`, environment credentials, or a workload role).

#### Resend

Required:

- `PDT_RESEND_API_KEY`

## The bundled examples

Run `pdt examples` to list them, then `pdt new <name> --from <example>` to copy one.

- `hello-world` logs one line. It shows the layout of `config.yml` and `run.py`, and `pdt init` puts a copy in every new empty project.
- `impossible-travel-report` reads recent Entra ID sign-ins and sends an email when one person signs in from two far-apart places too quickly: Dallas an hour ago, Paris now. The lookback window, the minimum distance, and the email addresses are settings in its `config.yml`.
- `monday-orphaned-account-report` reports active Monday users whose email address matches no active Entra ID user's `userPrincipalName`.

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

Set `PDT_PROJECT` to name the project folder directly, instead of letting pdt search upward. Deployed apps get it set for them.
