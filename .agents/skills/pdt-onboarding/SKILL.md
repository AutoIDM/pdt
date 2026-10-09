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

1. Run `docker info`. If it succeeds, Docker is ready. Go to step 2.
2. If Docker is installed but not running, start it, then wait until `docker info` succeeds:
   - macOS with Colima: `colima start`
   - macOS with Docker Desktop: `open -a Docker`
   - Windows: start Docker Desktop from the Start menu, or run `Start-Process "$env:ProgramFiles\Docker\Docker\Docker Desktop.exe"`
   - Linux: `sudo systemctl start docker`
3. If Docker is not installed, tell the user what you will install and ask once. Then:
   - macOS with Homebrew: `brew install colima docker docker-buildx`, then `mkdir -p ~/.docker/cli-plugins && ln -sfn "$(brew --prefix)/lib/docker/cli-plugins/docker-buildx" ~/.docker/cli-plugins/docker-buildx`, then `colima start`. This needs no administrator rights and no license.
   - Windows: `winget install -e --id Docker.DockerDesktop`. It asks for administrator rights. Tell the user that Docker Desktop is free only for personal use, education, and companies with fewer than 250 employees and less than $10 million in yearly revenue. If Windows asks for a restart, stop here and tell the user to restart and start again.
   - Linux: `curl -fsSL https://get.docker.com | sudo sh`, then `sudo systemctl enable --now docker` and `sudo usermod -aG docker $USER`. The user must log out and log in again before `docker` works without `sudo`.
   - Any other case: send the user to https://docs.docker.com/get-started/get-docker/ and wait until they say Docker is installed.
4. Run `docker buildx version`. pdt needs buildx for its builds. If it fails on macOS, run the `ln` command from step 3 again.

Check: `docker info` succeeds.

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
