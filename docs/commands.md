# Commands

| Command | What it does |
| --- | --- |
| `pdt init [DIR]` | create a project here, or in DIR |
| `pdt examples` | list the example apps bundled with pdt |
| `pdt new APP` | add an empty app to the project |
| `pdt list` | show every app with its schedule and platform; `--names` prints only the enabled app names; `--json` prints one object per app with `name`, `schedule`, `platform`, `enabled`, `paused`, and `error` (a config problem, or `null`) |
| `pdt validate` | check the config files and the required env vars; exits 1 when it finds a problem; `--json` prints `{"ok": ..., "problems": [{"app": ..., "message": ...}]}`, where `app` is `null` for a problem of the project or of more than one app |
| `pdt run APP` | run an app on this machine |
| `pdt run APP --deployed` | start one run of the deployed job now, on its platform |
| `pdt run APP --deployed --follow` | start one run of the deployed job, follow its log, and exit 0 when it succeeds or 1 when it fails |
| `pdt deploy APP` | deploy an app to its configured platform, then ask whether to start a run and follow its log |
| `pdt deploy APP --run` | deploy, then start a run and follow its log with no question |
| `pdt deploy APP --rebuild` | deploy, and build and push the image even when the app did not change |
| `pdt deploy APP --plan` | show the plan and the monthly cost estimate, then stop; asks nothing and takes each default that `--yes` takes; creates or changes no cloud resource, except that on Google Cloud it enables each required API that is not enabled, which costs nothing, so it can read and price the project; changes no `pdt.yml`, `config.yml`, or `.pdt/state`; signs in to nothing; installs no tool, such as PowerShell or the Google Cloud CLI; can write local caches: the uv cache of the Python packages of the provider script, the price cache of pdt, and a temporary folder of PowerShell scans that pdt deletes when it exits |
| `pdt deploy --all` | deploy every enabled app, in order; skips an app whose plan you decline; asks whether to skip and disable an app that fails; add `--yes --skip-failures` to run unattended |
| `pdt destroy APP` | remove everything deploy created |
| `pdt destroy APP --plan` | show what destroy deletes, then stop; asks nothing and deletes nothing; keeps the record of where the app is deployed in `.pdt/state`; signs in to nothing; installs no tool; on Google Cloud it enables each required API that is not enabled, which costs nothing, so it can read the project |
| `pdt destroy --all` | destroy every deployed app, in order; each app shows its plan and asks; asks whether to skip an app that fails; add `--yes --skip-failures` to run unattended |
| `pdt pause APP` | stop the deployed schedule from starting runs; writes `pause: true` into the app's `config.yml` |
| `pdt unpause APP` | let the schedule start runs again; writes `pause: false` |
| `pdt secrets APP` | show which `.env` values differ from the deployed app |
| `pdt secrets APP save` | send your `.env` values to the deployed app; the next run uses them |
| `pdt secrets APP get` | copy the deployed values into a `.env.<provider>` file |
| `pdt secrets APP set NAME` | put one value, read from stdin, into the deployed app's secrets |
| `pdt login APP` | sign in again to the app's platform |
| `pdt storage APP ls\|get\|query\|unlock\|destroy` | look at, fetch, query, unlock, or delete the app's stored files; `ls` shows the number of files and the size under each folder; `ls PATH --recursive` lists every file under a folder, and `ls --json` prints the list as JSON; on Windows, Tab completes the path after `ls` and `get`; `--open` opens the app's storage folder (File Explorer on Windows, the cloud console otherwise), and `get PATH --open` opens the saved file |
| `pdt runs APP` | list the deployed app's runs with each run's exit code: the 10 newest, or with `--since 3d` (or `12h`, `2w`, `2026-09-20`, `2026-09-20T14:00`) every run since then, `--span 1d` keeping only the runs within that long after `--since` and `--count N` keeping only the N newest; a run's number is its place among every run pdt can still find, so it stays the same whichever runs print; `--open` opens the runs in Task Scheduler on Windows, or in the cloud console |
| `pdt logs APP [N]` | read the log of run N as `pdt runs` numbers it; with no N, the newest run, `--failed` picks the newest failed run instead, and `--id ID` picks a run by the id `pdt runs` shows (repeat it to read several runs; their `--json` is then an object keyed by id); `--since`, `--span`, and `--count` limit which runs those two choose from, `--errors` leaves out DEBUG and INFO lines; the last 20 lines print, `--lines N` prints N instead, `--head` prints the first lines instead of the last, `--full` prints every line, and `--follow` keeps printing new lines, in time order, until the run ends and its log has the run's exit line; a running run's lines come from a live tail of its log (the container's log stream on Azure, a Cloud Logging tail on Google Cloud, CloudWatch Logs Live Tail on AWS), because the log store shows a line seconds to minutes after the job writes it; a run that is still running, or whose lines have not reached the log store yet, says so; on Windows, `pdt logs` names the log file it reads; `--open` opens the app's logs (the logs folder on Windows, the cloud console otherwise), or one run's log with N, `--failed`, or `--id` |
| `pdt logs APP --list` | list each run log, for the same runs as `pdt runs` (`--since`, `--span`, `--count`, and `--json` work the same way) |
| `pdt health [APP] [--all]` | show whether each app's last run succeeded; exits 1 when one failed; inside an app folder it checks only that app, and `--all` checks every app |
| `pdt gui` | open the project's dashboard in your browser: every app's health, each app's run history, and each run's log and files, with buttons for pause, unpause, and run now; `--background` keeps the server up after the command returns and `pdt gui --stop` ends it; `--port N` and `--no-browser` are there for the rare case |
| `pdt aws ...` | run the AWS CLI that pdt installs |
| `pdt az ...` | run the Azure CLI that pdt installs |
| `pdt gcloud ...` | run the Google Cloud CLI that pdt installs |
| `pdt completion [SHELL]` | turn on tab completion for a shell |

Inside an app folder, or any folder below it, leave `APP` off any command that takes one and pdt uses that app. For example, `pdt logs 3` in the `hello-world` folder reads run 3 of `hello-world`. Anywhere else, leave `APP` off or mistype it and pdt lists the apps in the project so you can pick one; a mistyped name also gets the closest app name. A mistyped command or option gets the closest one too, for example `pdt lgos` asks "Did you mean `pdt logs`?".

With `--json`, `pdt list`, `pdt validate`, `pdt runs`, `pdt logs`, `pdt health`, and `pdt storage APP ls` print only JSON on stdout, and every other line, such as an error or a sign-in question, on stderr. In a project with no enabled apps, `pdt runs`, `pdt logs`, `pdt health`, and `pdt storage APP ls` print `[]`. A command that has no JSON to print exits with a code other than 0 and prints nothing on stdout.

`pdt aws`, `pdt az`, and `pdt gcloud` hand your arguments straight to the cloud tool, and install it first if it is missing. For example, `pdt az account list`.
