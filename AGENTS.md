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
- `src/pdt/scaffold.py` owns `init`, `examples`, and `new`. `STARTER` names the example that `init` copies into an empty project.
- `src/pdt/deploy.py` is provider-neutral deploy and destroy. It validates, then dispatches to one module per provider.
- `src/pdt/deploy_<provider>.py` is one module per provider. Provider runtimes get their own module under the provider (`deploy_aws_lambda.py`, `deploy_azure_functions.py`). Code shared by the runtimes of one provider stays in that provider module.
- `src/pdt/deploy_common.py` holds code shared by every provider: `fail`, the `DOCKERFILE`, `gather_secrets`, and `stage_build_context`. A provider module imports from here. A provider module never imports from another provider module.
- `src/pdt/utils/` is code the user's apps import. It is public API. Changing it breaks every deployed app, so treat a change here as breaking.
- `src/pdt/examples/<name>/` ships inside the wheel. `pdt new` copies one into the user's project. An example never sets `name:` in its `config.yml`, because the copy takes the new folder's name.
- `tests/` runs with pytest and needs no network and no cloud account.

## Config rules

- The user's project holds `pdt.yml` with a `platform:` block of defaults for every app and an `apps:` list.
- An app directory holds `config.yml`. It configures only that app. It does not list apps. The two filenames stay different, or the upward walk stops inside an app folder.
- Merge order, lowest to highest: `pdt.yml` `platform:` defaults, the app's entry in the `pdt.yml` `apps:` list, the app directory's `config.yml`, environment variables. The app directory is more specific than the project.
- `schedule` and `timezone` exist only per app. They never exist in `pdt.yml`.
- Every config file passes the same validation.
- `platform.provider` selects the provider module. `platform.runtime` selects the runtime inside a provider (AWS: `lambda` default, `fargate`; Azure: `functions` default, `container_apps`). Both can be set in `pdt.yml` and overridden per app. Each provider with more than one runtime offers the same two shapes: a zip upload with no Docker as the default (AWS `lambda`, Azure `functions`) and a container (AWS `fargate`, Azure `container_apps`).

## CLI rules

- `src/pdt/cli.py` delegates each command to a module. Do not put provider or app logic in it.
- A command that needs a project calls `find_project()` and lets `ConfigError` reach `main`, which prints it. Do not print and return 1 in each command.
- `src/pdt/deploy.py` must stay provider-neutral. It does these steps, in order: load and validate config, check env vars, check the schedule, prepare cross-cutting items such as email auth, then dispatch. The only provider check in it is the dispatch on `platform.provider`.
- Dispatch every provider the same way. Do not add `if provider == "aws"` style branches. Adding a provider means adding `src/pdt/deploy_<provider>.py` and registering the name in `deploy.PROVIDERS`, `config.PROVIDERS`, and `scaffold.PROVIDER_CHOICES` plus `scaffold.PROVIDER_QUESTIONS`. Those lists must agree. A name in one and not another gives the user a config that validates and then fails at deploy.
- Every provider script takes the same command line: `deploy|destroy|login <app> [--yes] [--profile NAME]`. Options that apply to one provider only (for example `--profile`) pass through unchanged. A provider ignores options that do not apply to it.
- `pdt az` and `pdt gcloud` forward the rest of the command line to that provider's CLI, and install it first if it is missing. Add a passthrough only for a provider whose CLI pdt already manages.
- Dependencies: `pyproject.toml` declares only cross-cutting packages. A provider SDK such as `boto3` or `azure-cli` belongs in that provider module's PEP 723 script header, never in `[project.dependencies]`, because `deploy.py` dispatches with `uv run --script src/pdt/deploy_<provider>.py`. Use the same dispatch mechanism for every provider. A module that is only imported carries no script header.
- A provider script reaches the package through `sys.path.insert(0, Path(__file__).resolve().parent.parent)`. That resolves to `src/` in a clone and to site-packages after an install.
- The root validates config and env once. A provider module validates only provider-specific items (account, project, region, login). It does not repeat the root validation.

## Deploy and destroy rules (every provider)

- Deploy reconciles. It creates what is missing and updates what changed. Re-running after a failure is safe.
- Before it changes anything, deploy prints a plan (one line per action) and a monthly cost estimate. It then asks `Proceed? [y/N]`. `--yes` skips the question.
- Cost estimates use real price data from the provider's pricing API. Use the same assumptions on every provider (`ASSUMED_RUN_MINUTES` per run, the schedule's runs per month) so estimates compare one to one. Use past run data when it exists. If an API needed for the estimate is disabled, enable it and retry in the same deploy. Do not make the user deploy once without an estimate.
- Pick the cheapest adequate option by default (for example arm64 on AWS).
- Never copy package source into a build context. The `deploy_common.py` docstring describes the context and the secret layout every provider shares.
- The version pinned in a scaffolded `run.py` is the version that stays deployed. Upgrading pdt locally must not change a job already running.
- Name per-app resources `pdt-<app>`. Tag or label every created resource `managed-by=pdt`. The string `autoidm` appears nowhere in code, names, tags, or defaults. Only delete resources that carry that tag.
- Destroy removes everything deploy created for the app. When no other app still uses a shared resource (schedule group, cluster, image repository, service account), destroy removes that too. Do not leave resources behind. Do not use recovery windows or soft deletes; on Azure that means delete plus purge for Key Vaults and their secrets.
- A provider must not let the platform auto-create side resources (Application Insights, default Log Analytics workspaces, action groups). Create each one explicitly, tag it, print it in the plan, and delete it in destroy. After the last app is destroyed, the resource group or project is gone; destroy ends by listing anything that still remains.
- Destroy prints what it will delete before it deletes anything, and asks to proceed.
- Guide the user. If a required tool is missing, install it (see the guiding principle above). If a login or profile is missing, list the choices and ask. If a permission is missing, print the exact policy the user must add.
- Schedules are cron expressions in config. Each provider translates them to its own scheduler format.

## Anything written to disk outside the project

The tool downloads the Google Cloud CLI to the user's data folder (`~/.local/share/pdt`, or `%LOCALAPPDATA%\pdt`). Never write it beside the code. An installed package's folder is managed by `uv`, and an upgrade discards whatever is in it.

## Writing Markdown

Do not hard-wrap prose at a column. Write each paragraph and each list item as one long line and let the editor wrap it. A line break exists only where the document needs one: between blocks, inside a fenced code block, or between table rows. This keeps a one-word edit from reflowing a whole paragraph in the diff.

## Tests and CI

- `tests/` covers project discovery, config merge order, cron parsing and `runs_per_month`, and the scaffolding guards. Add a test with the rule it covers, in the same change.
- A test needs no network, no cloud account, and no installed CLI.
