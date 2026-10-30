# Projects and apps

## Set up a project

A project is a folder holding `pdt.yml`. Each app is a folder inside it that contains a `run.py`, or the `.ps1` scripts of a PowerShell app. Every command except `init` finds the project by looking in the current folder, then each folder above it.

```
pdt init my-jobs
```

`pdt init` asks where the project should live, which cloud you want, and which region. It warns you if you are about to create a project somewhere unwise, such as your home folder. The folder name is optional: `pdt init` with no name uses the current folder, and `pdt init DIR` uses `DIR`, creating it if it is not there yet. Add `--yes` to take the defaults and answer nothing.

Region is the only place setting it asks for. Your AWS account, Azure subscription, and Google Cloud project all come from your credentials the first time you deploy, and pdt writes the answer into `pdt.yml` so every later deploy checks against it.

To move a project to another provider, run `pdt init` in the project and pick the new provider. If apps are deployed, `pdt init` lists them and asks which to move: all, some, or none. For each app you move, it runs `pdt destroy` where the app is deployed, then updates `pdt.yml` to deploy to the new provider. Stored data will not be deleted. Each app that stays gets its current place written into its `config.yml`. An app whose own `config.yml` sets its provider does not move; edit that file to move it. Then run `pdt deploy <app>` for each app you moved.

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
| the AutoIDM registrations of this computer, from `pdt register` | `~/.local/share/pdt/registrations`, or `%ProgramData%\pdt\registrations` |
| cloud sign-in state | `~/.aws`, `~/.azure`, and `~/.config/gcloud`, as usual |

Set `PDT_PROJECT` to name the project folder directly, instead of letting pdt search upward. Deployed jobs get it set for them.

## Managed services from AutoIDM

When AutoIDM manages your project, `pdt.yml` names your customer ID and what pdt sends to AutoIDM:

```yaml
managed_services:
  customer: cus_7a1c09e4b2
  send_run_history: true    # or a list of app names; true when you leave it out
  send_logs: [my-report]    # or true; false when you leave it out
  send_artifacts: false     # or a list of app names, or true; false when you leave it out
```

- `send_run_history` sends the start and the end of each run, with its exit code and how long it took, and the schedule of each app at each deploy.
- `send_logs` sends the full output of each run when the run ends.
- `send_artifacts` sends the files the run left in its storage output folder, as one zip file, when the run ends. pdt skips a zip over 100 MiB and prints a warning.

A list sends only for the apps it names. pdt sends the same things for a scheduled run on every provider and for a `pdt run` on your computer. It sends nothing when the env var `CI` is set, so a CI job sends nothing. Without `managed_services`, pdt sends nothing.

Each computer that runs or deploys the project needs a registration for the customer. Run `pdt register --code CODE` once on it, with the code AutoIDM gives you; on Windows, run it in a terminal opened with Run as administrator. Run in a project folder whose `pdt.yml` has no `managed_services`, it adds `managed_services:` with the customer of the code. A computer can hold one registration for each customer. Until the computer has one, each pdt command prints one warning and sends nothing.

A cloud job cannot use the registration of your computer. The first deploy of an app to AWS, Azure, or Google Cloud makes a registration for the job and keeps it in the app's secret as `PDT_INSTALLATION_ID`, `PDT_INSTALLATION_KEY`, and `PDT_API_URL`. Later deploys keep it. `pdt secrets` never shows, copies, or changes these three values.

When AutoIDM cannot be reached, or answers with an error, pdt prints a warning and the run goes on. When AutoIDM refuses the registration, the run still does its work, then ends with exit code 77 and a message that names the registration, so your alerts report the run as failed. A run that failed for its own reason keeps its own exit code.
