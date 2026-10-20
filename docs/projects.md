# Projects and apps

## Set up a project

A project is a folder holding `pdt.yml`. Each app is a folder inside it that contains a `run.py`, or the `.ps1` scripts of a PowerShell app. Every command except `init` finds the project by looking in the current folder, then each folder above it.

```
pdt init my-jobs
```

`pdt init` asks where the project should live, which cloud you want, which region, and which time zone the schedules use. Then it offers to sign you in to that cloud with `pdt login`. It warns you if you are about to create a project somewhere unwise, such as your home folder. The folder name is optional: `pdt init` with no name uses the current folder, and `pdt init DIR` uses `DIR`, creating it if it is not there yet. Add `--yes` to take the defaults and answer nothing.

Region and time zone are the only settings it asks for. Your AWS account, Azure subscription, and Google Cloud project all come from your credentials the first time you deploy, and pdt writes the answer into `pdt.yml` so every later deploy checks against it.

To move a project to another provider, run `pdt init` in the project and pick the new provider. pdt replaces the `platform:` block of `pdt.yml` and keeps your apps and every other setting. It keeps the time zone, and drops the region, account, subscription, and project of the old provider. `pdt destroy` removes apps from the provider that `pdt.yml` names, so run `pdt destroy --all` before the move if apps are deployed. pdt warns you when they are. After the move, run `pdt deploy --all`.

pdt suggests the region nearest to you from your computer's time zone. If `pdt.yml` has no region when you first deploy, the deploy suggests one the same way, asks you to confirm it or type another, and saves your answer. With `--yes` it takes the suggestion and tells you which region it used.

Starting in an empty folder also gives you a working app called `hello-world`. Run it straight away:

```
cd my-jobs
pdt run hello-world
```

It writes one log line and nothing else. Edit `hello-world/run.py` to make it do real work, or delete the folder if you would rather start from one of the [bundled examples](../README.md#the-bundled-examples). A folder that already holds your own files gets no starter app.

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

## Where things live

| Item | Where |
| --- | --- |
| the `pdt` command and its code | the tool's own environment, from `uv tool install` |
| the example apps | inside the pdt package, copied out by `pdt new` |
| your apps, `pdt.yml`, `.env` | your project folder, under version control |
| the Google Cloud CLI pdt downloads | `~/.local/share/pdt/gcloud`, or `%LOCALAPPDATA%\pdt\gcloud` |
| the PowerShell 7 pdt downloads | `~/.local/share/pdt/pwsh`, or `%ProgramData%\pdt\pwsh` |
| the DuckDB files of the `pdt gui` SQL workbench | `~/.local/share/pdt/duckdb-wasm`, or `%LOCALAPPDATA%\pdt\duckdb-wasm` |
| what `pdt gui` fetched | `.pdt/gui.sqlite3` in your project folder |
| a Windows scheduled task's run logs and files | `%ProgramData%\pdt\<app>\logs` and `%ProgramData%\pdt\<app>\storage` |
| cloud sign-in state | `~/.aws`, `~/.azure`, and `~/.config/gcloud`, as usual |

Set `PDT_PROJECT` to name the project folder directly, instead of letting pdt search upward. Deployed jobs get it set for them.
