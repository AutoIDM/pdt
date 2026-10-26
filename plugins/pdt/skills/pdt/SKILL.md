---
name: pdt
description: Use when the user has a Python or PowerShell script that works on their own computer and wants it to run on a schedule or "in production", for example "run this script every morning", "schedule this in Azure", or "put this in production" on AWS, Azure, Google Cloud, or Windows Task Scheduler. Also use when the user asks to check, run, deploy, or remove an app in a pdt project (a folder that holds a pdt.yml file). The pdt command-line tool checks, runs, and deploys the script as a scheduled job in the user's own cloud account or on their Windows PC, and shows the plan and a monthly cost estimate before it deploys. Do not use for web apps, APIs, websites, long-running services, or jobs that already deploy through a CI/CD pipeline.
---

# Run a script on a schedule with pdt

pdt turns a script into a scheduled job. A pdt project is a folder that holds `pdt.yml`. Each app is a subfolder of the project with a `run.py` file or `.ps1` files, and an optional `config.yml` that sets the schedule.

## Rules

- Never read, print, or copy a `.env` file. It holds the user's secrets. When `pdt validate` names a missing value, ask the user to add it to the `.env` file.
- Never run `pdt destroy`, `pdt pause`, `pdt unpause`, `pdt storage APP destroy`, `pdt storage APP unlock`, `pdt secrets APP set`, `pdt secrets APP save`, or `pdt secrets APP get` unless the user asks for that action.
- Never run `pdt deploy APP --yes` before the user has seen the output of `pdt deploy APP --plan` and agreed in the chat.
- Never run `pdt destroy APP --yes` before the user has seen the output of `pdt destroy APP --plan` and agreed in the chat.
- A browser sign-in is the user's step. When a command reports a missing sign-in, ask the user to run `pdt login APP` in their own terminal. Continue after they confirm.

## 1. Check that pdt is installed

Run `pdt --version`. If the command is not found, ask the user for permission to install it. After they agree, run `uv tool install pdt-cli`. If `uv` is also missing, give the user the uv install page: https://docs.astral.sh/uv/getting-started/installation/

## 2. Find or create the project

Look for `pdt.yml` in the current folder and in the folders above it. If you find one, run `pdt list` to see its apps.

If there is no project, ask the user to run `pdt init` in their own terminal, in the folder that will hold the project. `pdt init` asks where the jobs run, and only the user can answer that.

To add an app to the project:

- `pdt examples` lists the example apps. `pdt new APP --from EXAMPLE` copies one into the folder `APP`.
- For a Python script, run `pdt new APP`. pdt runs `APP/run.py`, so put the user's code in its `main` function, or copy the user's `.py` files into `APP` and call them from `main`. Add each package that the code imports to the `dependencies` list at the top of `run.py`.
- For PowerShell scripts, run `pdt new APP --from powershell-hello-world`. Delete `APP/hello.ps1` and copy the user's `.ps1` files into `APP`. Do not add a `run.py`. With a `run.py` and no `requirements.psd1`, pdt runs the folder as a Python app.

## 3. Check and run the app on this computer

1. Run `pdt validate`. It checks the config files and the environment variables that the code reads. Fix each problem in the file and key that it names.
2. Ask the user before you run `pdt run APP`, unless the user asked for that run. It runs the app once on this computer with the user's `.env` file, so it can change real data. Show the user the result.
3. Do not deploy an app that fails here. Fix it first.

## 4. Show the plan, then deploy

1. Run `pdt deploy APP --plan`. It prints each action and the monthly cost estimate, and then stops. It deploys nothing, asks nothing, and changes no config file. On Google Cloud, it enables each required Google Cloud API that is not enabled, which costs nothing. The output names any default it used, such as the schedule or the region.
2. Show the user the plan lines and the cost estimate. Ask whether to deploy.
3. Only after the user agrees in the chat, run `pdt deploy APP --yes`.

When the plan stops on a missing sign-in, follow the sign-in rule above. When it stops on a missing permission, show the user the policy or role that pdt prints.

## 5. Check the deployed job

- `pdt run APP --deployed --follow` starts one run now and shows its log. Ask the user first, because a run does real work.
- `pdt runs APP` lists the recent runs and their exit codes.
- `pdt logs APP` shows the log of the newest run. `pdt logs APP 2` shows the run before it.
- `pdt health` shows whether the last run of each deployed app succeeded.
- Add `--json` to `pdt runs`, `pdt logs`, `pdt health`, `pdt list`, or `pdt validate` to get output that is easier to read in a script.

## 6. Remove a deployed app

Do this only when the user asks to remove the app from the cloud.

1. Run `pdt destroy APP --plan`. It prints what `pdt destroy APP` removes, and then stops. It changes nothing.
2. Show the user the plan. When the plan lists resources to remove, ask whether to remove them.
3. Only after the user agrees in the chat, run `pdt destroy APP --yes`.
