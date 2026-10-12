---
name: pdt-onboarding
description: Walk a new user through installing pdt and running their first job. Use when someone asks to install pdt, set up pdt, get started with pdt, or follows the install prompt in the pdt README.
---

# pdt onboarding

Work with the user to onboard into pdt. Don't assume the user is a software engineer or has familiarity with software development processes and practices. When communicating, keep things simple and easy to understand.

## General guidelines

- One step at a time. Ask one question, then wait for the answer.
- Run each command for the user. Show the command and say what it does in one sentence before you run it.
- After each step, run its check. Go to the next step only when the check passes.
- When a check fails, read the error, fix the cause, and run the check again. Do not skip the step.

## 1. Install pdt

Check to see if pdt is already installed or needs to be upgraded. Follow the README.md in pdt to install pdt.

1. Run `pdt --version`. Find the newest release with `uv tool list --outdated` or on https://pypi.org/project/pdt-cli/.
2. If pdt is older than the newest release, upgrade it without a question, then tell the user which version you installed:
   - Windows, installed with winget: `winget upgrade AutoIDM.pdt`
   - Otherwise: `uv tool upgrade pdt-cli`
3. If pdt is not installed:
   - Windows: `winget install AutoIDM.pdt`. It also installs uv. If winget is missing or fails, use the uv steps below.
   - macOS and Linux, or Windows without winget: install uv with the command in the README Quick start, then run `uv tool install pdt-cli` and `uv tool update-shell`.
4. Your terminal does not see the new PATH. Load it before you run pdt:
   - Windows PowerShell: `$env:Path = [Environment]::GetEnvironmentVariable('Path','User') + ';' + [Environment]::GetEnvironmentVariable('Path','Machine')`
   - macOS and Linux: `export PATH="$HOME/.local/bin:$PATH"`

Check: `pdt --version` prints the newest release.

### Docker

pdt builds each cloud app with Docker on this computer. An app that runs on a Windows PC does not need Docker.

1. Do not install Docker yourself. Never install Docker Desktop.
2. pdt gets Docker ready during the first cloud deploy. It starts an installed Docker that is not running. When Docker is not installed, pdt asks once, then installs a free Docker and starts it:
   - macOS with Homebrew: Colima
   - Windows x64: Rancher Desktop
   - Linux: Docker Engine
3. If pdt stops with a Docker message, show that message to the user and do what it says.

Check: after the first cloud deploy, `docker info` succeeds.

## 2. Introduction

pdt is a lightweight orchestrator. It deploys apps, such as tasks, reports, and integrations, to the user's own AWS, Azure, or Google Cloud account or to a Windows PC, and runs them on a schedule. Example: every Monday at 8 a.m., an app emails a list of the Entra ID users who have not signed in for 90 days.

1. Introduce pdt and give an example of how it can be used.
2. Provide the user choices to find out their goal: migrate an existing script, try an example, or something else.
3. Follow the associated workflow based on their choice.

## 3. Create a project

<!-- TODO(I32.A): pdt init, where jobs run, region -->

Check: `pdt run hello-world` succeeds.

## 4. Follow the user's goal

### Migrate an existing script

Use the `migrate-app` skill.

### Try an example

<!-- TODO(I32.A): pdt examples, pdt new --from -->

### Something else

<!-- TODO(I32.A) -->

## 5. Deploy

<!-- TODO(I32.A): Docker, sign-in, cost estimate, pdt deploy, pdt run --deployed, pdt logs -->

Check: `pdt health APP` shows that the last run succeeded.
