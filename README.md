# pdt

Run scheduled jobs — reports, integrations, automations — and deploy them to AWS, Azure, Google Cloud, or Windows Task Scheduler with one command.

## Install

```
uv tool install pdt-cli
```

That puts a `pdt` command on your PATH. To update it later, run `uv tool upgrade pdt-cli`.

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
| `pdt list` | show every app with its schedule and provider |
| `pdt validate` | check the config files and the required env vars |
| `pdt run APP` | run an app on this machine |
| `pdt deploy APP` | deploy an app to its configured platform |
| `pdt destroy APP` | remove everything deploy created |
| `pdt login APP` | sign in again to the app's cloud provider |
| `pdt az ...` | run the Azure CLI that pdt installs |
| `pdt gcloud ...` | run the Google Cloud CLI that pdt installs |

`pdt az` and `pdt gcloud` hand your arguments straight to the cloud tool, and install it first if it is missing. For example, `pdt az account list`.

## Running pdt from CI

Every command reads its values from the environment it runs in. A build server sets them as CI variables, and no `.env` file is needed.

- A `.env` file is a convenience for a person working on their own machine. A value that is already in the environment wins over the same name in a `.env` file.
- `PDT_ENV_JSON` holds every value as one JSON object, for a CI system that keeps one secret instead of many. For example, `PDT_ENV_JSON={"PDT_TOKEN": "abc", "PDT_SMTP_USER": "reports@example.com"}`.
- pdt creates no `.env` file on a build server. It looks for the `CI` variable that build systems set, and for a missing terminal. Email authorization must therefore be done first: run `pdt run APP` once on a machine with a browser, then copy `PDT_SMTP_OAUTH_CACHE_B64` (or `PDT_GRAPH_MAIL_CACHE_B64` for Microsoft Graph) from your `.env` into the CI variables.
- `pdt deploy APP` and `pdt destroy APP` ask before they change anything. Add `--yes` so they proceed without asking.

## Choosing where jobs run

Set `platform:` in `pdt.yml` for every app, or in an app's own `config.yml` for one app. An app's own file wins.

### Azure

```yaml
platform:
  provider: azure
  subscription: 00000000-0000-0000-0000-000000000000
  region: eastus
  resource_group: pdt
```

`subscription` is optional. When it is missing or wrong, the deploy asks you to choose one.

### AWS

```yaml
platform:
  provider: aws
  region: us-east-1
  # Written for you on the first deploy, from your credentials.
  account: "123456789012"
```

### Google Cloud

```yaml
platform:
  provider: google-cloud
  region: us-central1
  project: my-starter-project
```

`project` is optional. When it is missing or wrong, the deploy lists your projects and asks you to choose one, then writes your answer here.

### Bringing your own Dockerfile

Every cloud provider runs a job as a container: AWS on Fargate, Azure on Container Apps Jobs, Google Cloud on Cloud Run Jobs. The deploy builds an image for each app from a generated Dockerfile. It copies the app folder and `pdt.yml` into `/workspace`, installs the script-header dependencies of `run.py` with `uv sync --script`, and runs `run.py` as the entrypoint.

Put a `Dockerfile` in the app folder to build the image your own way, for example to add system packages or to install a heavy tool at build time instead of on every run. The build context is the same as the generated one: the app folder under its own name, next to `pdt.yml`. Start from the generated file:

```dockerfile
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim
COPY . /workspace
WORKDIR /workspace/my-app
ENV PDT_PROJECT=/workspace
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
    timezone: local
    schedule: daily
```

The job runs as the SYSTEM account. Windows accepts these schedules:

- `hourly`, `daily`, `weekly`, `monthly`, and `yearly`
- `*/N * * * *` minute intervals where `N` divides 60
- one fixed time daily
- one fixed time on selected weekdays
- one fixed time on selected days of selected months

Other cron forms are rejected, because they do not translate to Windows Task Scheduler.

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
| the example apps | inside the pdt package, copied out by `pdt new` |
| your apps, `pdt.yml`, `.env` | your project folder, under version control |
| the Google Cloud CLI pdt downloads | `~/.local/share/pdt/gcloud`, or `%LOCALAPPDATA%\pdt\gcloud` |
| cloud sign-in state | `~/.azure` and `~/.config/gcloud`, as usual |

Set `PDT_PROJECT` to name the project folder directly, instead of letting pdt search upward. Deployed jobs get it set for them.

## Writing your own app

An app is a folder with a `run.py` that has a `main()` function. It declares its own dependencies in a script header, and pdt is one of them:

```python
# /// script
# requires-python = ">=3.12"
# dependencies = ["pdt-cli[apps]==0.1.0"]
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

## Photo sync

Copy BambooHR employee photos onto Entra ID accounts. The app matches BambooHR `workEmail` to Entra `mail` and `userPrincipalName`. A unique match is required. BambooHR is the source of truth. The app does not clear an Entra photo when BambooHR has none.

This sample uses BambooHR. `tap-bamboohr` already extracts photos. `target-azuread` already writes them with `PUT /users/{id}/photo/$value`. Other HRIS sources can use the same match-and-put steps.

Create the app in your project, then run it:

```
pdt new photo-sync --from photo-sync
pdt run photo-sync
```

Set `bamboohr_subdomain` and `photo_size` in `photo-sync/config.yml`. Set `dry_run: true` to log matches without writing photos. Credentials are documented in `photo-sync/config.yml` and `photo-sync/env.template`.

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
