---
name: pdt
description: Use when the user wants a script or a command to run on its own, without them, for example "make my backup.py run every night", "run this script every morning", "schedule this report", "set up a cron job for this", "put this in production", or "run this in my own cloud account" on AWS, Azure, Google Cloud, or Windows Task Scheduler. pdt turns any script into a scheduled job in your cloud. It builds, runs, and deploys scheduled jobs, and everything it makes lives in the user's own account. Also use when the user asks to check, run, deploy, or remove an app in a pdt project (a folder that holds a pdt.yml file). Do not use for GUI apps, web apps, websites, APIs, or long-running services, or for jobs that already deploy through a CI/CD pipeline.
---

# Turn a script into a scheduled job with pdt

pdt turns any script into a scheduled job in the user's cloud. It builds, runs, and deploys scheduled jobs to AWS, Azure, Google Cloud, or Windows Task Scheduler. There are no servers to run, and everything it makes lives in the user's own account.

A pdt project is a folder that holds `pdt.yml`. Each app is a subfolder of the project. A Python app has a `run.py`. A PowerShell app has `.ps1` files. The app's `config.yml` sets the schedule and the env vars that the app needs.

Do each step yourself. Ask the user only for what they must decide, and ask in the chat. Tell the user in one sentence what each command does before you run it.

## Rules

- Never read, print, or copy a `.env` file. It holds the user's secrets. When `pdt validate` names a missing value, ask the user to add it to the `.env` file.
- Never run `pdt destroy`, `pdt pause`, `pdt unpause`, `pdt storage APP destroy`, `pdt storage APP unlock`, `pdt secrets APP set`, `pdt secrets APP save`, or `pdt secrets APP get` unless the user asks for that action.
- Ask the user before you run `pdt run APP`, unless the user asked for that run. The run uses the user's `.env` file, so it can change real data.
- Never run `pdt deploy APP --yes` before the user has seen the output of `pdt deploy APP --plan` and agreed in the chat.
- Never run `pdt destroy APP --yes` before the user has seen the output of `pdt destroy APP --plan` and agreed in the chat.

## When pdt fits

pdt fits a job that starts, does its work, and stops:

- It runs on a schedule: `hourly`, `daily`, `weekly`, `monthly`, `yearly`, or a 5-field cron expression. It can also run once when the user asks, with `pdt run APP --deployed`.
- It is a Python script, a set of PowerShell scripts, or a command that a Python `run.py` can start.
- It reads its secrets from env vars. pdt keeps the values in `.env` on the user's computer and in the cloud's secret store.
- It keeps files between runs in the storage folder that pdt gives each app. Other files that a run writes are gone when the run ends.

## When pdt does not fit

Tell the user that pdt is the wrong tool, and do not use it, for these:

- A GUI app. A deployed job has no screen and no signed-in user. On Windows Task Scheduler, the job runs as the SYSTEM account.
- A web app, a website, an API, or another service that waits for requests. No other computer can connect to a deployed job.
- A long-running service, or a job that must keep running. On Azure and on Windows Task Scheduler, pdt stops a run after 30 minutes. Plan for each run to finish in less than 30 minutes on every provider.
- A job that needs a large amount of memory. pdt gives each run 512 MB of memory on AWS and 1 GiB on Azure.

## 1. Install pdt

Run `pdt --version` and `uv --version`. When both work, go to step 2.

When one or both are missing, ask the user once: "pdt is not installed. May I install uv and pdt now?" Name only the tools that are missing. After the user agrees, install uv first and pdt second.

On macOS and Linux:

```
curl -LsSf https://astral.sh/uv/install.sh | sh
source $HOME/.local/bin/env
uv tool install pdt-cli
uv tool update-shell
```

On Windows, in PowerShell:

```powershell
winget install --id astral-sh.uv -e
$env:Path = [Environment]::GetEnvironmentVariable('Path','Machine') + ';' + [Environment]::GetEnvironmentVariable('Path','User')
uv tool install pdt-cli
uv tool update-shell
$env:Path = [Environment]::GetEnvironmentVariable('Path','Machine') + ';' + [Environment]::GetEnvironmentVariable('Path','User')
pdt --version
```

Skip the uv lines when uv is already installed. Each of your shell commands can start in a new shell, so put `source $HOME/.local/bin/env` (macOS and Linux) or the `$env:Path = ...` line (Windows) before each `uv` or `pdt` command in this session. The user's terminal windows that were already open do not find `pdt` until the user opens a new window.

pdt installs the other tools that it needs itself. Do not install them for it.

## 2. Ask what the job does

Ask these questions in one message. Skip each question that the user already answered:

1. Which script or command should run? Where is it?
2. How often should it run, and at what time?
3. Where should it run: Microsoft Azure, Amazon Web Services, Google Cloud, or this Windows PC with Task Scheduler?
4. Which secrets or settings does it read, such as passwords, API keys, or URLs?

When the answers show that pdt does not fit, tell the user why, and stop.

## 3. Find or create the project

Look for `pdt.yml` in the current folder and in the folders above it. When you find one, run `pdt list` to see its apps, and go to step 4.

When there is no project, create one. Use the folder the user names, or `pdt-jobs` in the current folder:

1. Run `pdt init pdt-jobs --yes`. It creates the project and asks nothing. In a new folder, it also adds a `hello-world` app.
2. In `pdt.yml`, replace the `#platform:` comment lines with the provider the user chose: `azure`, `aws`, `google-cloud`, or `windows`. For example:

   ```yaml
   platform:
     provider: azure
   ```

   Add `region:` only when the user named a region. When there is no region, the deploy plan suggests the region nearest to the user and names it.

## 4. Create the app

Pick a short app name from the job, such as `nightly-backup`.

### A PowerShell script

1. Run `pdt new APP --from powershell-hello-world`.
2. Delete `APP/hello.ps1`, and copy the user's `.ps1` files into `APP`.

Do not add a `run.py`. With a `run.py` and no `requirements.psd1`, pdt runs the folder as a Python app. pdt finds the modules that the scripts import, and the env vars that they read.

### Anything else: write a wrapper run.py

1. Run `pdt new APP`. It creates `APP/run.py` and `APP/config.yml` from the `hello-world` example.
2. Copy the user's files into `APP`. A deploy sends only the app folder and `pdt.yml` to the cloud, so the job cannot reach a file outside the app folder.
3. In `APP/run.py`, keep the script header and the config and env checks at the top of `main`. Replace the `log(...)` line with a call to the user's code, and return its exit code:

   ```python
   import subprocess

   result = subprocess.run([sys.executable, "backup.py"], cwd=app_dir)
   log("info", "backup.py finished", code=result.returncode)
   return result.returncode
   ```

   Change the command for what the user has:

   - A `.py` script: `[sys.executable, "backup.py", ...]`. Or import the script's function and call it.
   - A Python module: `[sys.executable, "-m", "package.module", ...]`.
   - A command from a Python package, such as `dbt`: `["dbt", "build"]`.
   - A shell script: `["sh", "job.sh"]` or `["bash", "job.sh"]`.

4. Add each Python package that the code imports or calls to the `dependencies` list in the script header of `run.py`. Keep the `pdt-cli[apps]` entry. pdt installs this list on the user's computer and in the cloud image.

In the cloud, the job runs in a Debian Linux image with Python 3.12 and uv. It has no other language or program, such as Node.js, Java, or a browser. When the user's command needs one, tell the user, and add a `Dockerfile` to the app folder that starts from the generated one in the pdt [platforms guide](https://github.com/AutoIDM/pdt/blob/main/docs/platforms.md#bringing-your-own-dockerfile). On Windows Task Scheduler, the job runs on that PC, so it can use the programs that are installed on it.

### Files that must stay between runs

Each run starts with an empty folder. When the job must keep a file for the next run or for the user, such as the last result to compare against, keep it in the app's storage folder with `pdt.utils.storage`. See [Keeping files between runs](https://github.com/AutoIDM/pdt/blob/main/docs/storage.md).

## 5. Set the schedule and the secrets

1. In `APP/config.yml`, set `schedule:` to the time the user asked for: `hourly`, `daily`, `weekly`, `monthly`, `yearly`, or a 5-field cron expression such as `"0 2 * * *"` for 2:00 every night. On Windows Task Scheduler, use a shorthand, an interval of minutes that divides 60 (`*/15 * * * *`), or one fixed time.
2. Under `env:` in `APP/config.yml`, list the name of each env var that the code reads under `required:` or `optional:`. pdt finds the `os.environ` reads in the `.py` files and the `$env:` reads in the `.ps1` files itself. List the names that a shell script or another program reads.
3. Ask the user to add the value of each env var to the `.env` file in the project folder. Do not ask for the values in the chat. `pdt deploy` sends the values to the cloud's secret store.

## 6. Check and run the app on this computer

1. Run `pdt validate`. It checks the config files and the env vars that the code reads. Fix each problem in the file and key that it names.
2. Ask the user, and then run `pdt run APP`. It runs the app once on this computer, the same way that the cloud runs it. Show the user the result.
3. Do not deploy an app that fails here. Fix it first.

## 7. Show the plan, then deploy

1. Run `pdt deploy APP --plan`. It prints each action and the monthly cost estimate, and then stops. It deploys nothing, asks nothing, and changes no config file. On Google Cloud, it enables each required Google Cloud API that is not enabled, which costs nothing. The output names any default it used, such as the schedule or the region.
2. Show the user the plan lines and the cost estimate. Ask whether to deploy.
3. Only after the user agrees in the chat, run `pdt deploy APP --yes`.

When a command stops on a missing sign-in, run `pdt login APP`. It opens the sign-in page of the cloud in the user's browser. Tell the user to sign in there, and continue after the command ends. When `pdt login APP` stops because it must ask a question in a terminal, ask the user to run `pdt login APP` in their own terminal, and continue after they confirm.

When a command stops on a missing permission, show the user the policy or role that pdt prints.

## 8. Check the deployed job

- `pdt run APP --deployed --follow` starts one run now and shows its log. Ask the user first, because a run does real work.
- `pdt runs APP` lists the recent runs and their exit codes.
- `pdt logs APP` shows the log of the newest run. `pdt logs APP 2` shows the run before it.
- `pdt health` shows whether the last run of each deployed app succeeded.
- Add `--json` to `pdt runs`, `pdt logs`, `pdt health`, `pdt list`, or `pdt validate` to get output that is easier to read in a script.

Tell the user that `pdt gui` shows the runs, logs, and files of each app in the browser, and that pdt can send alerts to Slack, Teams, or email when a run fails.

## 9. Remove a deployed app

Do this only when the user asks to remove the app from the cloud.

1. Run `pdt destroy APP --plan`. It prints what `pdt destroy APP` removes, and then stops. It changes nothing.
2. Show the user the plan. When the plan lists resources to remove, ask whether to remove them.
3. Only after the user agrees in the chat, run `pdt destroy APP --yes`.
