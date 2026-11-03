# Choosing where jobs run

Set `platform:` in `pdt.yml` for every app, or in an app's own `config.yml` for one app. An app's own file wins. `timezone` may live under `platform:` as the default for every app, and an app's own `timezone` overrides it.

A deploy to AWS, Azure, or Google Cloud builds each app's image in one of two places, and it says which place and why:

- When `docker info` answers, Docker on your computer builds the image and uploads it to the cloud's image registry.
- When Docker is not installed or not running, the cloud builds the image: ACR Tasks on Azure, Cloud Build on Google Cloud, and CodeBuild on AWS. The deploy uploads the app folder and `pdt.yml`, without `.env` and the other secret files, and the cloud builder builds them with the same Dockerfile, the same base image, and the same CPU type as a build on your computer.

When a build fails, the deploy shows the error and stops.

Set `docker-build` to choose the place yourself. Put it at the top level of `pdt.yml` for every app, or at the top level of an app's `config.yml` (or its `apps:` entry) for one app. The app's value wins.

```yaml
docker-build: auto   # Docker on this computer when it answers, else the cloud (the default)
# docker-build: local  # Docker on this computer only; the deploy stops when Docker is not running
# docker-build: cloud  # the cloud only
```

## Azure

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

## AWS

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

## Google Cloud

```yaml
platform:
  provider: google-cloud
  region: us-central1
  project: my-starter-project
```

`project` is optional. When it is missing or wrong, the deploy lists your projects and asks you to choose one, then writes your answer here.

## Bringing your own Dockerfile

Every cloud provider runs a job as a container (see [Primitives](../README.md#primitives)). The deploy builds an image for each app from a generated Dockerfile. It installs the script-header dependencies of `run.py` with `uv sync --script`, then copies the app folder and `pdt.yml` into `/workspace`, and runs `run.py` as the entrypoint. The install uses only the script header, so a deploy after a code or config change reuses the installed dependencies.

Put a `Dockerfile` in the app folder to build the image your own way, for example to add system packages or to install a heavy tool at build time instead of on every run. The build context is the same as the generated one: the app folder under its own name, next to `pdt.yml`. Start from the generated file:

```dockerfile
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim@sha256:e5b65587bce7de595f299855d7385fe7fca39b8a74baa261ba1b7147afa78e58
COPY . /workspace
WORKDIR /workspace/my-app
ENV PDT_PROJECT=/workspace NO_COLOR=1 DBT_USE_COLORS=false
RUN uv sync --script run.py
ENTRYPOINT ["sh", "-c", "uv run --script run.py; code=$?; echo \"pdt: exit $code\"; exit $code"]
```

A `.dockerignore` in the app folder keeps files out of the image. Write its patterns relative to the app folder (`.meltano`, `output`, `*.csv`); pdt moves them to the root of the build context for you. `.env` and the other secret files never reach the context, with or without a `.dockerignore`.

The `ENTRYPOINT` ends each run's output with the line `pdt: exit N`, which `pdt runs` reads for the run's exit code. Keep that line when you change the rest of the file.

A `RUN --mount=type=ssh` step in your own `Dockerfile` can install a private git dependency with your computer's SSH agent. Only a build with Docker on your computer has the agent, so a deploy that would build such an app in the cloud stops and names the step.

The Windows provider never builds an image, so `pdt validate` reports a Dockerfile in an app that uses it.

## Windows Task Scheduler

```yaml
platform:
  provider: windows

apps:
  - name: my-report
    schedule: daily
```

The Windows provider runs each job on the PC's local time. It needs no `timezone`, and ignores one that is set.

The job runs as the SYSTEM account. Set `run_as` to run it as a group managed service account instead, for example so the job reads and changes Active Directory with the same account as your other tools:

```yaml
platform:
  provider: windows
  run_as: EXAMPLE\svc-pdt$
```

Put `run_as` in the `platform:` block of `pdt.yml` for every app, or in an app's own `config.yml` for one app. The name ends in `$`, because pdt supports only a group managed service account: Task Scheduler gets its password from the domain, so pdt stores no password. Write the name without quotes, or in single quotes, because YAML reads a backslash in double quotes as an escape. `pdt validate` reports a value that is not a name ending in `$`. Before it changes anything, `pdt deploy` asks Active Directory whether the account is a group managed service account (objectClass `msDS-GroupManagedServiceAccount`), and stops when Windows cannot find the account, when the account is another kind, or when the PC cannot reach a domain controller. The check needs no RSAT tools.

The PC must be allowed to retrieve the account's password: a domain administrator adds the PC, or a group that holds it, to the account's `PrincipalsAllowedToRetrieveManagedPassword`, and `Test-ADServiceAccount` on the PC then returns `True`. Without that, the task does not start. The account also needs the **Log on as a batch job** right on the PC, in Local Security Policy or a group policy. pdt sets neither of these, and the deploy names the batch right. The deploy gives the account everything else it needs: full control of the app's folder (described below), and read access to the app's own folder, to the files `pdt.yml`, `.env`, `pyproject.toml`, `uv.toml`, and `.python-version` in the project folder when they exist (uv reads its settings from them), to pdt, to uv when uv is not on the system PATH, and to the AutoIDM installation key `%ProgramData%\pdt\installation.key` when it exists. The account cannot read the other apps' folders. A PowerShell app gets the pinned PowerShell in `%ProgramData%\pdt\pwsh` when no `pwsh.exe` is on the system PATH, because the account sees only that PATH, and the account gets read access to it. A deploy after you change or remove `run_as` registers the task again with the new account. When you upgrade pdt or uv, deploy again, so the account can read the new files.

pdt writes each right it gives the account, and that the account did not have before, to `%ProgramData%\pdt\grants\<app>.json`. Each deploy and destroy makes Administrators the owner of `%ProgramData%\pdt` and of the `grants` folder, and only SYSTEM and Administrators can change the `grants` folder, so the account cannot delete the record. A deploy with another account, or with no `run_as`, removes the rights that the record lists for the old account, and `pdt destroy` removes all of them. A right that another app's record lists for the same account stays. When the record is missing or pdt cannot read it, or when an `icacls` command fails, pdt cannot be sure that the old account lost its rights, so it prints one `icacls` command per folder or file that removes them. Run them in an administrator PowerShell, unless another app runs as that account. A failed `pdt destroy` keeps the record, so the next `pdt destroy` removes the rights.

These limits apply:

- Run one `pdt deploy` or `pdt destroy` at a time on a PC. Two at once can both read the same records, and one can remove a right that the other still needs.
- When the PC stops after pdt changes a folder's rights and before pdt writes the record, the record does not list those rights, and pdt does not remove them later.
- A deploy made with an earlier test version of this feature kept its record in the app folder. pdt does not read that record. Remove those rights with `icacls` yourself.
- The record lists folders and files, not the rights. When the account had its own explicit right on a folder or file before pdt gave it a right there, for example to read it, the record does not list that folder or file, and the account keeps the right pdt gave it, such as full control, after a destroy.

Each run writes one event to the Windows Application log, source `pdt`: event 1000 when it succeeds, event 1001 when it fails.

Windows accepts these schedules:

- `hourly`, `daily`, `weekly`, `monthly`, and `yearly`
- `*/N * * * *` minute intervals where `N` divides 60
- one fixed time daily
- one fixed time on selected weekdays
- one fixed time on selected days of selected months

Other cron forms are rejected, because they do not translate to Windows Task Scheduler.

Each app keeps its run data in `%ProgramData%\pdt\<app>\`. pdt keeps its own files in `%ProgramData%\pdt` under the names `pwsh`, `versions`, `wheels`, `grants`, and `installation.key`, so `pdt validate` reports a Windows app with one of those names. The task writes one log file per run to the `logs\` folder in it, named by the run's UTC start time, and `pdt runs <app>` and `pdt logs <app>` read those files. The app's own files (see [Keeping files between runs](storage.md)) live in the `storage\` folder next to it. Deploy creates the folder and gives the SYSTEM account (and the `run_as` account) full control and you modify rights, so `pdt storage <app> destroy` works without an administrator. `pdt destroy <app>` removes the task and the `logs\` folder and keeps `storage\`. A run is stopped after 30 minutes, the same limit as the cloud providers, and a run that is still going when the next one is due is left alone; the new start is skipped.
