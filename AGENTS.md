# AGENTS.md

This repo is the source of `pdt-cli`, a tool IT teams install. They use it to create their own project of small scheduled jobs (reports, integrations, automations), and to validate, run, and deploy those jobs with the help of an AI agent. Assume the user is not a cloud expert and not a software engineer.

## Guiding principle: ease of use

Ease of use and simplification of the process is the top guiding principle. Every decision in this repo is measured against it.

- The user runs one command, for example `pdt deploy <app>`. The command does everything else. It never tells the user to go and install, configure, or look something up first.
- A required tool installs itself. Prefer a PyPI package in the provider script header (`boto3`, `azure-cli`) so `uv` installs it with no user step. If no package exists, download a pinned copy after one `[y/N]` question (see `src/pdt/gcloud_sdk.py`). Never print "install X and run again".
- A missing login gets one `[y/N]` question, then the command opens the browser login itself and continues.
- A missing permission prints the exact policy or role the user must add.
- Prompts use plain words. The reader is an IT administrator. Say "Which AWS region should hold your jobs?", not "platform.region is required".
- An error names the file and the key the user must change.
- Every cloud provider is at parity. Deploying to AWS, Google Cloud, or Azure asks the same number of questions and needs the same user knowledge. When one provider gains a convenience, add it to the other providers in the same change. When one provider needs a manual step the others do not, that is a bug. The `windows` provider runs on the user's own PC, so it asks nothing and has no cloud parity to hold.
- The simplest behavior is the default. A convenience is on for every app, and a user turns it off with one key. A user never has to opt in.
- A shared resource follows one pattern on every provider: the same name prefix, the same tag, and the same lifecycle scope. If one provider keeps a resource in its own group, bucket, or account scope, every provider does.
- Fewer config keys beat more. A key exists only when the user must choose a value. Names for shared resources get stable defaults; they are not config.
- `pdt init` asks only what pdt cannot find out for itself. If a provider's CLI or SDK can name the account, subscription, or project, deploy discovers it, writes it back with `config.save_platform_key`, and checks against it every time after that. Region is the only setting a cloud provider asks for, because it decides where the data lives.
- `pdt init` in a folder holding nothing also creates the `hello-world` starter app, so a new user has something that runs before they write anything. A folder that already holds the user's files gets no starter.

## The repo is not a project

Two directory trees exist and they are never the same tree.

- **This repo** holds the tool. It has no `pdt.yml`, so no command ever mistakes it for a project.
- **The user's project** is any folder holding `pdt.yml`. The user creates it with `pdt init`. Their apps live there, under their own version control.

`pdt.config.find_project()` walks up from the working folder to the nearest `pdt.yml`. `PDT_PROJECT` overrides the walk. Never derive the project from `__file__`; after an install that path is inside site-packages.

Both install routes must keep working, and a change is not done until both do:

- `uv tool install pdt-cli` puts `pdt` on the PATH.
- A clone runs `./pdt` or `.\pdt.bat`, which call `uv run --project <clone> pdt`.

## Layout

- `pyproject.toml` declares the `pdt-cli` distribution and the `pdt` command. Core dependencies are the cross-cutting ones. The `apps` extra holds what a user's `run.py` needs on top of those.
- `pdt` and `pdt.bat` are clone shims only. They hold no logic.
- `src/pdt/cli.py` parses arguments and delegates. It holds no provider logic.
- `src/pdt/config.py` finds the project, then loads, merges, and validates config. A rule that both a prompt and validation need lives here once, as a function returning a message or `""` (see `aws_account_problem`). `save_platform_key` is the one way to write a value back into a config file; it edits text so comments survive, and quotes the value so an id with a leading zero does not become a number.
- `src/pdt/scaffold.py` owns `init`, `examples`, and `new`. `STARTER` names the example that `init` copies into an empty project. `init` also writes `AGENTS.md` (and a `CLAUDE.md` pointing at it) into the user's project.
- `src/pdt/deploy.py` is provider-neutral deploy and destroy. It validates, then dispatches to one module per provider.
- `src/pdt/deploy_<provider>.py` is one module per provider. AWS and Azure keep the job itself in a module under the provider (`deploy_aws_fargate.py`, `deploy_azure_container_apps.py`); login, secrets, prices, and everything else the job shares with `login` and `destroy` stay in the provider module.
- `src/pdt/deploy_common.py` holds code shared by every provider: `fail`, the `DOCKERFILE`, `gather_secrets`, and `stage_build_context`. A provider module imports from here. A provider module never imports from another provider module.
- `src/pdt/terraform.py` owns Terraform configuration, plans, durable deployment records, and locks. Provider files generate native Terraform JSON through `terraform_<provider>.py`. PDT downloads its pinned Terraform version itself.
- Cloud deployments bootstrap protected remote state and operation locks. AWS uses S3, Azure uses Blob Storage, and Google Cloud uses GCS. Windows uses project-local state under `.pdt/terraform` because it has no cloud scope. `--unlock` recovers a lock only after its operation stopped.
- Terraform state must not contain application secret payloads. Terraform owns secret containers and access rules. Provider code writes secret payloads directly to the provider's secret service after Terraform applies those resources. State storage stays protected because native providers can persist generated infrastructure credentials.
- `src/pdt/utils/` is code the user's apps import. It is public API. Changing it breaks every deployed app, so treat a change here as breaking.
- `src/pdt/examples/<name>/` ships inside the wheel. `pdt new` copies one into the user's project. An example never sets `name:` in its `config.yml`, because the copy takes the new folder's name.
- `tests/` runs with pytest and needs no network and no cloud account.

## Config rules

- The user's project holds `pdt.yml` with a `platform:` block of defaults for every app and an `apps:` list.
- An app directory holds `config.yml`. It configures only that app. It does not list apps. The two filenames stay different, or the upward walk stops inside an app folder.
- Merge order, lowest to highest: `pdt.yml` `platform:` defaults, the app's entry in the `pdt.yml` `apps:` list, the app directory's `config.yml`, environment variables. The app directory is more specific than the project.
- `schedule` and `timezone` exist only per app. They never exist in `pdt.yml`.
- Every config file passes the same validation.
- `platform.provider` selects the provider module. Every cloud provider runs a job the same way: a container image built from the app folder, on AWS Fargate, Azure Container Apps Jobs, or Google Cloud Run Jobs. There is no zip runtime and no `platform.runtime` key; validation tells a user who still sets one to remove it. Do not add a second way to run a job on a provider.

## CLI rules

- `src/pdt/cli.py` delegates each command to a module. Do not put provider or app logic in it.
- Every line for the user goes through `from pdt import console`, never `print`. The vocabulary is in the `src/pdt/console.py` docstring, and ruff rule `T20` fails the build on a `print` call. The only exceptions are `utils/log.py` and `utils/send_email.py`, whose output is an app's cloud log.
- A command that needs a project calls `find_project()` and lets `ConfigError` reach `main`, which prints it. Do not print and return 1 in each command.
- `src/pdt/deploy.py` must stay provider-neutral. It does these steps, in order: load and validate config, check env vars, check the schedule, prepare cross-cutting items such as email auth, then dispatch. The only provider check in it is the dispatch on `platform.provider`.
- Dispatch every provider the same way. Do not add `if provider == "aws"` style branches. Adding a provider means adding `src/pdt/deploy_<provider>.py` and registering the name in `deploy.PROVIDERS`, `config.PROVIDERS`, and `scaffold.PROVIDER_CHOICES` plus `scaffold.PROVIDER_QUESTIONS`. Those lists must agree. A name in one and not another gives the user a config that validates and then fails at deploy.
- Every provider script takes the same command line: `deploy|destroy|login <app> [--yes] [--profile NAME]`. Options that apply to one provider only (for example `--profile`) pass through unchanged. A provider ignores options that do not apply to it.
- `pdt aws`, `pdt az`, and `pdt gcloud` forward the rest of the command line to that provider's CLI, and install it first if it is missing. Add a passthrough only for a provider whose CLI pdt already manages.
- Dependencies: `pyproject.toml` declares only cross-cutting packages. A provider SDK such as `boto3` or `azure-cli` belongs in that provider module's PEP 723 script header, never in `[project.dependencies]`, because `deploy.py` dispatches with `uv run --script src/pdt/deploy_<provider>.py`. Use the same dispatch mechanism for every provider. A module that is only imported carries no script header.
- A provider script reaches the package through `sys.path.insert(0, Path(__file__).resolve().parent.parent)`. That resolves to `src/` in a clone and to site-packages after an install.
- The root validates config and env once. A provider module validates only provider-specific items (account, project, region, login). It does not repeat the root validation.

## Deploy and destroy rules (every provider)

- Deploy reconciles. It creates what is missing and updates what changed. Re-running after a failure is safe.
- Before it changes anything, deploy prints a plan (one line per action) and a monthly cost estimate. It then asks `Proceed? [y/N]`. `--yes` skips the question.
- Cost estimates use real price data from the provider's pricing API. Use the same assumptions on every provider (`ASSUMED_RUN_MINUTES` per run, the schedule's runs per month) so estimates compare one to one. Use past run data when it exists. If an API needed for the estimate is disabled, enable it and retry in the same deploy. Do not make the user deploy once without an estimate.
- Pick the cheapest adequate option by default (for example arm64 on AWS).
- Never copy package source into a build context. The `deploy_common.py` docstring describes the context and the secret layout every provider shares.
- An app folder may hold its own `Dockerfile`. Every cloud provider builds from it through `deploy_common.write_dockerfile`, which puts it (or the generated `DOCKERFILE`) at the root of the staged context; `config.dockerfile_problem` is the one rule for when the file would be ignored. Do not read the Dockerfile in a provider module.
- The version pinned in a scaffolded `run.py` is the version that stays deployed. Upgrading pdt locally must not change a job already running.
- Name per-app resources `pdt-<app>`. Tag or label every created resource `managed-by=pdt`. The string `autoidm` appears nowhere in code, names, tags, or defaults. Only delete resources that carry that tag.
- Destroy removes everything deploy created for the app. When no other app still uses a shared resource (schedule group, cluster, image repository, service account), destroy removes that too. Do not leave resources behind. Do not use recovery windows or soft deletes; on Azure that means delete plus purge for Key Vaults and their secrets.
- A provider must not let the platform auto-create side resources (Application Insights, default Log Analytics workspaces, action groups). Create each one explicitly, tag it, print it in the plan, and delete it in destroy. After the last app is destroyed, the resource group or project is gone; destroy ends by listing anything that still remains.
- Terraform configurations own infrastructure resources. Provider CLI calls may package or upload code, build or remove images, deliver secret payloads, authenticate, or inspect live resources for safe adoption and cleanup.
- Destroy prints what it will delete before it deletes anything, and asks to proceed.
- Guide the user. If a required tool is missing, install it (see the guiding principle above). If a login or profile is missing, list the choices and ask. If a permission is missing, print the exact policy the user must add.
- Schedules are cron expressions in config. Each provider translates them to its own scheduler format.

## Anything written to disk outside the project

The tool downloads the Google Cloud CLI to the user's data folder (`~/.local/share/pdt`, or `%LOCALAPPDATA%\pdt`). Never write it beside the code. An installed package's folder is managed by `uv`, and an upgrade discards whatever is in it.

## Writing Markdown

Do not hard-wrap prose at a column. Write each paragraph and each list item as one long line and let the editor wrap it. A line break exists only where the document needs one: between blocks, inside a fenced code block, or between table rows. This keeps a one-word edit from reflowing a whole paragraph in the diff.

## Making a change

Never commit on `master`. Start every change in a git worktree on its own branch (in a Claude Code session, use EnterWorktree before editing), push the branch, and open a merge request with `glab mr create`. Changes land through the MR.

## Tests and CI

- `tests/` covers project discovery, config merge order, cron parsing and `runs_per_month`, and the scaffolding guards. Add a test with the rule it covers, in the same change.
- A test needs no network, no cloud account, and no installed CLI.
- `ci/claude_task.py` runs a Claude Code task from GitLab CI on the team's Claude subscription. A task is a folder `ci/claude-tasks/<name>/` holding `task.yml` (model, budget, timeout, tool allowlist), `prompt.md`, an optional `system.md`, and two optional hooks: `select` prints a JSON list of items before Claude runs (one Claude session per item) and `check` judges each result afterwards, printing `ok`, `needs_human`, or `error`. Items run `parallel` at a time (default 1), each in its own git worktree that is deleted afterwards, so a task's hooks and prompt must not assume the main checkout. A CI job extends `.claude-task` in `.gitlab-ci.yml` and sets `CLAUDE_TASK`. The runner verifies the token before doing anything and exits 1 with a plain message when it is missing or rejected. Exit code 2 means an item needs a person and shows as a warning on the pipeline. Reports land in the `claude-task-report/` artifact.
- The first task is `rebase-mrs`: after every merge to master, `select_mrs.py` rebases each open MR onto it with plain git and pushes with lease when that is clean; only an MR whose rebase hits a conflict goes to Claude, who resolves it, runs the tests, and force-pushes with lease. `check_mrs.py` confirms the branch sits on master with no added commits and comments on the MR when a person must look. The label `no-autorebase` opts an MR out. The jobs need the masked, protected CI variables `CLAUDE_TOKEN` (a one-year token from `claude setup-token`; it must be renewed yearly) and `GITLAB_TOKEN` (project access token with `api` and `write_repository`). How to create them is in the handbook, `developer.md`, "Claude Code in CI".
- The `score-mrs` task scores every open MR into `tier::simple`, `tier::review`, or `tier::architectural`. `select_mrs.py` sets a floor tier from path rules (unknown files are `review`) and merges an already-scored `tier::simple` MR once its pipeline passed on the head commit with no conflicts and no unresolved discussions. Claude reads the diff and may raise the floor, never lower it; with no usable verdict a simple floor becomes review. `check_mrs.py` sets the label, comments the reasons, and reports `needs_human` for an architectural MR so the pipeline warns and the team discusses it. Scores are keyed on the diff content, so a rebase does not re-score. A Draft is scored once and then left alone until it leaves Draft, when it is scored again only if its diff changed; a Draft is never merged. It runs on a master pipeline whose `mode` variable is `score_mrs`: a project webhook on merge request events triggers one through the pipeline trigger API when an MR is opened, edited, or marked ready, and a daily schedule sweeps every open MR. The webhook has a custom template that passes the MR number, the event's action, and its Draft change as the pipeline variables `MR_IID`, `MR_ACTION`, `MR_DRAFT_BEFORE`, and `MR_DRAFT_AFTER`; the `workflow` rules at the top of `.gitlab-ci.yml` let a trigger pipeline exist only when the MR opens or leaves Draft, so a push, a rebase, a label, an edit, or a close creates no pipeline at all. A webhook run scores the MR in `MR_IID` alone. A changed diff waits for the daily sweep, which has no `MR_IID` and covers every open MR. The template is set on the webhook under Settings > Webhooks, as described in the handbook. It does not run on merges to master. A pipeline that sets `mode` runs only that job; `test`, `lint`, `smoke`, and `rebase-mrs` skip it; a web run with `mode=score_mrs` is a dry run unless the form also sets `DRY_RUN=false`. The trigger token and webhook are set up as described in the handbook, `developer.md`, "Claude Code in CI". Merging uses `GITLAB_TOKEN`, so that token's role must be allowed to merge into master. The rules live in `select_mrs.py`; change them with a test in `ci/tests/test_score_task.py`.
- Scheduled and manual pipelines select a job with the `mode` variable. A daily schedule with `mode=close_stale_drafts` runs only `ci/close_stale_drafts.py`. It closes any Draft MR that no person has touched for more than 5 business days, comments "Stale Draft MR, closing" on it, and posts the list to `#pdt` in Slack. "Touched" means opened, a commit authored, or a note written by anyone other than the `GITLAB_TOKEN` bot user, so a rebase from `rebase-mrs` or a label or comment from `score-mrs` does not keep a Draft alive; the MR's `updated_at` is never used. It uses the same `GITLAB_TOKEN` as the Claude tasks plus the masked CI variable `SLACK_WEBHOOK_URL`. Outside a scheduled pipeline `DRY_RUN` defaults to true, so a manual web run lists candidates and changes nothing unless the form sets `DRY_RUN=false`.
