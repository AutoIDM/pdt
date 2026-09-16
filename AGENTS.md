# AGENTS.md

This repo is the source of `pdt-cli`, a tool IT teams install. They use it to create their own project of small scheduled apps (reports, integrations, automations), and to validate, run, and deploy those apps with the help of an AI agent. Assume the user is not a cloud expert and not a software engineer.

## Guiding principle: ease of use

Ease of use is the top guiding principle. Every decision in this repo is measured against it.

- The user runs one command, for example `pdt deploy <app>`. The command does everything else. It never tells the user to install, configure, or look something up first.
- A required tool installs itself. Prefer a PyPI package in the provider script header (`boto3`, `azure-cli`) so `uv` installs it with no user step. If no package exists, download a pinned copy with no question, in CI and on a user's computer alike (see `src/pdt/gcloud_sdk.py`). Never print "install X and run again".
- A missing login gets one `[y/N]` question, then the command opens the browser login itself and continues. A missing permission prints the exact policy or role the user must add.
- Prompts use plain words for an IT administrator. Say "Which AWS region should hold your jobs?", not "platform.region is required". An error names the file and the key the user must change.
- Every cloud provider is at parity. Deploying to AWS, Google Cloud, or Azure asks the same number of questions and needs the same user knowledge. When one provider gains a convenience, add it to the others in the same change. When one provider needs a manual step the others do not, that is a bug. The `windows` provider runs on the user's own PC, so it asks nothing and has no parity to hold.
- A shared resource follows one pattern on every provider: the same name prefix, the same tag, and the same lifecycle scope.
- The simplest behavior is the default. A convenience is on for every app, and a user turns it off with one key. A user never opts in.
- Fewer config keys beat more. A key exists only when the user must choose a value. Names for shared resources get stable defaults; they are not config.
- `pdt init` asks only what pdt cannot find out for itself. Region is the only setting a cloud provider asks for, because it decides where the data lives. Deploy discovers the account, subscription, project, or AWS profile from the credentials, writes it back with `config.save_platform_key`, and checks against it every time after that.
- `pdt init` in an empty folder also creates the `hello-world` starter app, so a new user has something that runs before they write anything. A folder that already holds the user's files gets no starter.

## The repo is not a project

- **This repo** holds the tool. Its root has no `pdt.yml`, so no command ever mistakes it for a project. The one `pdt.yml` in the repo is `verify/pdt.yml`, the live test project described under `## verify/`.
- **The user's project** is any folder holding `pdt.yml`. The user creates it with `pdt init`. Their apps live there, under their own version control.
- `config.find_project()` walks up from the working folder to the nearest `pdt.yml`; `PDT_PROJECT` overrides the walk. Never derive the project from `__file__`: after an install that path is inside site-packages.
- Both install routes must keep working, and a change is not done until both do: `uv tool install pdt-cli` puts `pdt` on the PATH, and a clone runs `./pdt` or `.\pdt.bat`.

## Layout

- `pyproject.toml` declares only cross-cutting dependencies. The `apps` extra holds what a user's `run.py` needs on top of those. A provider SDK such as `boto3` or `azure-cli` belongs in that provider module's PEP 723 script header, because `deploy.py` dispatches with `uv run --script src/pdt/deploy_<provider>.py`. A module that is only imported carries no script header.
- `src/pdt/config.py` finds the project, then loads, merges, and validates config. A rule that both a prompt and validation need lives here once, as a function returning a message or `""` (see `aws_account_problem`). `save_platform_key` is the one way to write a value back into a config file; it edits text so comments survive, and quotes and escapes the value so an id with a leading zero does not become a number. It holds a lock on a `.lock` file next to the config file while it reads and rewrites it, and replaces the file through a temporary file, so two first deploys at once or a crash mid-write cannot corrupt it.
- `src/pdt/scaffold.py` owns `init`, `examples`, and `new`. `STARTER` names the example that `init` copies into an empty project. `init` also writes `AGENTS.md` (and a `CLAUDE.md` pointing at it) into the user's project.
- `src/pdt/deploy_<provider>.py` is one module per provider. AWS and Azure keep the job itself in a module under the provider (`deploy_aws_batch.py`, `deploy_azure_container_apps.py`); login, secrets, prices, and everything the job shares with `login` and `destroy` stay in the provider module.
- `src/pdt/deploy_common.py` holds code shared by every provider. A provider module imports from here and never from another provider module.
- `src/pdt/utils/` is code the user's apps import. It is public API. Changing it breaks every deployed app, so treat a change here as breaking.
- `src/pdt/examples/<name>/` ships inside the wheel. An example never sets `name:` in its `config.yml`, because `pdt new` copies it and the copy takes the new folder's name.

## Config rules

- Merge order, lowest to highest: `pdt.yml` `platform:` defaults, the app's entry in the `pdt.yml` `apps:` list, the app directory's `config.yml`, environment variables. The app directory is more specific than the project.
- An app directory holds `config.yml`, never `pdt.yml`. The two filenames stay different, or the upward walk stops inside an app folder.
- Every cloud provider runs a job one way: a container image built from the app folder, on AWS Batch (Fargate), Azure Container Apps Jobs, or Google Cloud Run Jobs. There is no zip runtime and no `platform.runtime` key; validation tells a user who still sets one to remove it. Do not add a second way to run a job on a provider.

## CLI rules

- `src/pdt/cli.py` parses arguments and delegates. It holds no provider or app logic.
- Every line for the user goes through `from pdt import console`, never `print`. The vocabulary is in the `src/pdt/console.py` docstring, and ruff rule `T20` fails the build on a `print` call. The only exceptions are `utils/log.py` and `utils/send_email.py`, whose output is an app's cloud log.
- A command that needs a project calls `find_project()` and lets `ConfigError` reach `main`, which prints it. Do not print and return 1 in each command.
- `src/pdt/deploy.py` stays provider-neutral. It does these steps, in order: load and validate config, check env vars, check the schedule, prepare cross-cutting items such as email auth, then dispatch on `platform.provider`. That dispatch is its only provider check, and there is no `if provider == "aws"` style branch anywhere.
- Adding a provider means adding `src/pdt/deploy_<provider>.py` and registering the name in `deploy.PROVIDERS`, `config.PROVIDERS`, `scaffold.PROVIDER_CHOICES`, and `scaffold.PROVIDER_QUESTIONS`. Those lists must agree, or the user gets a config that validates and then fails at deploy.
- Every provider script takes the same command line: `deploy|destroy|login|secrets|storage|runs|logs <app> [--yes]`. A setting that one provider needs (an AWS profile, an Azure subscription, a Google Cloud project) is a `platform:` key, never a command-line option, so every provider is driven the same way.
- The root validates config and env once. A provider module validates only provider-specific items (account, project, region, login).
- `pdt aws`, `pdt az`, and `pdt gcloud` forward the rest of the command line to that provider's CLI. Add a passthrough only for a provider whose CLI pdt already installs.

## Deploy and destroy rules (every provider)

- Deploy reconciles. It creates what is missing and updates what changed. Re-running after a failure is safe.
- Before it changes anything, deploy prints a plan (one line per action) and a monthly cost estimate, then asks `Proceed? [y/N]`. Destroy prints what it will delete, then asks. `--yes` skips the question.
- Cost estimates use real price data from the provider's pricing API and the same assumptions on every provider (`ASSUMED_RUN_MINUTES` per run, the schedule's runs per month), so estimates compare one to one. Use past run data when it exists. If an API needed for the estimate is disabled, enable it and retry in the same deploy. Never make the user deploy once without an estimate.
- Pick the cheapest adequate option by default (for example arm64 on AWS).
- Never copy package source into a build context. The `deploy_common.py` docstring describes the context, the secret layout, and the Dockerfile handling every provider shares. `config.dockerfile_problem` is the one rule for when an app's own Dockerfile would be ignored; do not read the Dockerfile in a provider module.
- The version pinned in a scaffolded `run.py` is the version that stays deployed. Upgrading pdt locally must not change a job already running.
- Name per-app resources `pdt-<app>`. Tag or label every created resource `managed-by=pdt`, and only delete resources that carry that tag. The string `autoidm` appears nowhere in code, names, tags, or defaults.
- Destroy removes everything deploy created for the app. When no other app still uses a shared resource (schedule group, cluster, image repository, service account), destroy removes that too. Do not use recovery windows or soft deletes; on Azure that means delete plus purge for Key Vaults and their secrets. After the last app is destroyed, the resource group or project is gone, and destroy ends by listing anything that still remains.
- The data store (`pdt-data-<suffix>`, tagged `pdt-lifecycle: retain`) is the one resource that outlives its apps. Only `pdt storage <app> destroy` deletes an app's folder in it.
- On Azure the Container Apps environment and its Log Analytics workspace are shared by every pdt project in the subscription, one environment per region in the resource group `pdt-shared`, because a subscription's environment quota is small. Destroy releases them only when no job in the subscription uses the environment, and never touches an environment the user names in `platform.environment`. Each deployed app holds a `CanNotDelete` management lock named for it on the environment and on the project's ACR, taken before the image build, so a destroy that runs at the same time cannot delete either. Destroy removes its own locks first, re-reads the users right before each shared delete, and reports a lock refusal as kept rather than as a failure.
- A provider must not let the platform auto-create side resources (Application Insights, default Log Analytics workspaces, action groups). Create each one explicitly, tag it, print it in the plan, and delete it in destroy.

## Anything written to disk outside the project

The tool downloads the Google Cloud CLI to the user's data folder (`~/.local/share/pdt`, or `%LOCALAPPDATA%\pdt`). Never write it beside the code: an installed package's folder is managed by `uv`, and an upgrade discards whatever is in it.

## verify/

`verify/` is a pdt project used as a live test. It deploys to real cloud accounts, so it is the one directory in this repo that holds a `pdt.yml`. `verify/README.md` describes the run and the CI variables it needs.

- The `apps:` list in `verify/pdt.yml` is the matrix and the single source of truth. Add a target by adding a row there.
- The app directories are generated. Never edit `verify/<app>/run.py` or `verify/<app>/config.yml` by hand. Edit `verify/scripts/templates/` and run `uv run --with pyyaml python verify/scripts/sync_apps.py`.
- Never pin `pdt-cli` in `verify/.gitlab-ci.yml`. Each job installs the wheel the `build` job produced, so a run tests the commit it belongs to.
- Every run asserts the account is empty before the first deploy and after the last destroy. A run that starts on a dirty account fails instead of hiding the leftovers.
- The cloud jobs create and destroy real resources and cost real money. They are `interruptible: false`, because a cancelled run leaks what it made.

## Writing Markdown

Do not hard-wrap prose at a column. Write each paragraph and each list item as one long line and let the editor wrap it. A line break exists only where the document needs one: between blocks, inside a fenced code block, or between table rows. This keeps a one-word edit from reflowing a whole paragraph in the diff.

## Making a change

Never commit on `master`. Start every change in a git worktree on its own branch (in a Claude Code session, use EnterWorktree before editing), push the branch, and open a merge request with `glab mr create`. Changes land through the MR.

## Tests and CI

- `tests/` runs with pytest and needs no network, no cloud account, and no installed CLI. Add a test with the rule it covers, in the same change.
- `ci/claude_task.py` runs a Claude Code task from GitLab CI on the team's Claude subscription. Its docstring describes a task folder (`ci/claude-tasks/<name>/`), the `select` and `check` hooks, and the exit codes. The two tasks are `rebase-mrs` (rebases every open MR after a merge to master; the label `no-autorebase` opts an MR out) and `score-mrs` (labels every open MR `tier::simple`, `tier::review`, or `tier::architectural` and merges a `tier::simple` MR once its pipeline passes). Each hook's docstring holds its rules; change a rule with a test in `ci/tests/`.
- Scheduled, webhook, and manual pipelines select one job with the `mode` variable (`score_mrs`, `close_stale_drafts`), and the other jobs skip that pipeline. A web run is a dry run unless the form also sets `DRY_RUN=false`. Every webhook event must get a pipeline that runs a job: the `score-mrs` job rules run it only when the MR opens or leaves Draft, and every other event (a push, a rebase, a label, an edit, a close) runs the empty `score-mrs-skip` job instead. That empty job must exist, because the trigger API answers HTTP 400 when workflow rules filter a pipeline out or no job matches, GitLab counts a 4xx as a webhook failure and disables the hook after four in a row, and events that arrive while it is disabled are lost. Never filter trigger pipelines with `workflow` rules. The `score-mrs` webhook, its custom template, and the trigger token are set up as described in the handbook, `developer.md`, "Claude Code in CI".
- The CI jobs need the masked, protected variables `CLAUDE_TOKEN` (a one-year token from `claude setup-token`; it must be renewed yearly), `GITLAB_TOKEN` (a project access token with `api` and `write_repository`, whose role must be allowed to merge into master because `score-mrs` merges with it), and `SLACK_WEBHOOK_URL` for `ci/close_stale_drafts.py`. How to create them is in the handbook, `developer.md`, "Claude Code in CI".
