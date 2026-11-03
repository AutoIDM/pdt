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
FROM ghcr.io/astral-sh/uv:python3.13-trixie-slim@sha256:e586c1c65b7071aec78f02c5587c76c5011159cd76b3d6f1255f40e48e850abc
RUN pip uninstall -y --root-user-action=ignore pip
COPY . /workspace
WORKDIR /workspace/my-app
ENV PDT_PROJECT=/workspace NO_COLOR=1 DBT_USE_COLORS=false
RUN uv sync --script run.py && rm -rf "$(uv python dir)"/*/lib/python*/site-packages/pip*
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

Without `host`, the jobs run on this PC. To run them on another Windows PC, set `host:` under `platform:` to that PC's name, for example `host: pc01.example.com` or `host: admin@pc01.example.com`. Each app can name its own PC. Every pdt command then works on that PC, from Windows, macOS, or Linux. pdt signs in to the PC as an administrator. It tries SSH first, and then WinRM over HTTPS (port 5986) with a certificate your computer trusts. pdt never uses WinRM over HTTP. It says which one it used and why the other did not work. `pdt login <app>` does only this check. A domain administrator can sign in over both. A local administrator account can sign in over SSH. Over WinRM, Windows gives administrator rights only to the built-in Administrator account, and pdt does not change that setting. The PC needs access to the internet, because uv downloads Python and the app's packages there. pdt sends the app and its own code to the PC at each command, and puts the pinned uv on the PC when it is missing. `pdt secrets <app> get` and `pdt storage <app> get` save the file on your computer, and `--open` with `pdt storage <app> get` opens it there. Other `--open` forms show a place on the PC, so they stop with a message. `pdt deploy <app> --plan` runs uv from the upload folder when the PC has no pinned uv, and installs nothing.

The Windows provider runs each job on the PC's local time. It needs no `timezone`, and ignores one that is set.

The job runs as the SYSTEM account.

Each run writes one event to the Windows Application log, source `pdt`: event 1000 when it succeeds, event 1001 when it fails.

Windows accepts these schedules:

- `hourly`, `daily`, `weekly`, `monthly`, and `yearly`
- `*/N * * * *` minute intervals where `N` divides 60
- one fixed time daily
- one fixed time on selected weekdays
- one fixed time on selected days of selected months

Other cron forms are rejected, because they do not translate to Windows Task Scheduler.

Each app keeps its run data in `%ProgramData%\pdt\<app>\`. Deploy copies the app to the `app\` folder in it, without `.env` files and the other files that never go into an image, and copies pdt itself to `runtime\`. The task runs that copy. So a change to your project reaches the job at the next `pdt deploy`, as on the cloud providers. Deploy stores the app's env vars in `secrets.json` in the same folder, which only SYSTEM and Administrators can read, and the task reads them from there. `pdt secrets <app>` compares, saves, gets, and sets those values; when your terminal is not elevated, Windows asks for administrator rights once to read the values, and once more when `save` or `set` writes them. The task runs the pinned uv in `%ProgramData%\pdt\bin\`, and keeps its cache in `%ProgramData%\pdt\uv-cache\`. The task writes one log file per run to the `logs\` folder, named by the run's UTC start time, and `pdt runs <app>` and `pdt logs <app>` read those files. The app's own files (see [Keeping files between runs](storage.md)) live in the `storage\` folder next to it. Deploy creates the folder so that only SYSTEM and Administrators can change it, because SYSTEM runs what it holds, and gives you modify rights on `logs\` and `storage\`, so `pdt storage <app> destroy` works without an administrator. A deploy copies the new release next to the old one and swaps it in at the end, so a failed deploy leaves the last release in place. `pdt destroy <app>` removes the task, `app\`, `runtime\`, `secrets.json`, and the `logs\` folder, and keeps `storage\`. A run is stopped after 30 minutes, the same limit as the cloud providers, and a run that is still going when the next one is due is left alone; the new start is skipped.
