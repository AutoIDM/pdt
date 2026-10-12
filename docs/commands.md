# Commands

Back to the [README](../README.md).

| Command | What it does |
| --- | --- |
| `pdt init [DIR]` | create a project here, or in DIR; `--yes` takes the defaults and asks nothing |
| `pdt examples` | list the example apps bundled with pdt |
| `pdt new APP --from EXAMPLE` | add an app to the project |
| `pdt new APP --from-scripts` | write `requirements.psd1` and `run.py` for the `.ps1` files in a PowerShell app, so you can edit how they run |
| `pdt list` | show every app with its schedule and platform; `--names` prints only the enabled app names; `--json` prints one object per app with `name`, `schedule`, `platform`, `enabled`, `paused`, and `error` (a config problem, or `null`) |
| `pdt validate` | check the config files and the required env vars; exits 1 when it finds a problem; `--json` prints `{"ok": ..., "problems": [{"app": ..., "message": ...}]}`, where `app` is `null` for a problem of the project or of more than one app |
| `pdt run APP` | run an app on this machine |
| `pdt run APP --deployed` | start one run of the deployed job now, on its platform |
| `pdt deploy APP` | deploy an app to its configured platform |
| `pdt deploy --all` | deploy every enabled app, in order; asks whether to skip and disable an app that fails; add `--yes --skip-failures` to run unattended |
| `pdt destroy APP` | remove everything deploy created |
| `pdt pause APP` | stop the deployed schedule from starting runs; writes `pause: true` into the app's `config.yml` |
| `pdt unpause APP` | let the schedule start runs again; writes `pause: false` |
| `pdt secrets APP [diff]` | show which `.env` values differ from the deployed app |
| `pdt secrets APP save` | send your `.env` values to the deployed app; the next run uses them |
| `pdt secrets APP get` | copy the deployed values into a `.env.<provider>` file |
| `pdt secrets APP set NAME` | put one value, read from stdin, into the deployed app's secrets |
| `pdt secrets APP save --yes` | `save` asks before it changes the deployed secrets, and `get` asks before it replaces an existing file; add `--yes` to either one to skip the question |
| `pdt login APP` | sign in again to the app's platform |
| `pdt storage APP ls|get|query|unlock|destroy` | look at, fetch, query, unlock, or delete the app's stored files; `ls PATH --recursive` lists every file under a folder |
| `pdt runs APP` | list the deployed app's runs with each run's exit code: the 10 newest, or with `--since 3d` (or `12h`, `2w`, `2026-09-20`, `2026-09-20T14:00`) every run since then, `--span 1d` keeping only the runs within that long after `--since` and `--count N` keeping only the N newest; a run's number is its place among every run pdt can still find, so it stays the same whichever runs print; `--json` prints JSON for a script or an agent |
| `pdt logs APP [N]` | read the log of run N as `pdt runs` numbers it; with no N, the newest run, `--failed` picks the newest failed run instead, and `--id ID` picks a run by the id `pdt runs` shows (repeat it to read several runs; their `--json` is then an object keyed by id); `--since`, `--span`, and `--count` limit which runs those two choose from, `--errors` leaves out DEBUG and INFO lines; the last 20 lines print, `--lines N` prints N instead, `--head` prints the first lines instead of the last, `--full` prints every line, and `--follow` keeps printing new lines until the run ends and its log store has caught up; a run that is still running, or whose lines have not reached the log store yet, says so; `--json` prints JSON for a script or an agent |
| `pdt health [APP] [--all]` | show whether each app's last run succeeded; exits 1 when one failed; inside an app folder it checks only that app, and `--all` checks every app; `--json` prints JSON for a script or an agent |
| `pdt gui` | open the project's dashboard in your browser: every app's health, each app's run history, and each run's log and files, with buttons for pause, unpause, and run now; `--background` keeps the server up after the command returns and `pdt gui --stop` ends it; `--port N` and `--no-browser` are there for the rare case |
| `pdt aws ...` | run the AWS CLI that pdt installs |
| `pdt az ...` | run the Azure CLI that pdt installs |
| `pdt gcloud ...` | run the Google Cloud CLI that pdt installs |
| `pdt completion [SHELL]` | turn on tab completion for `bash`, `zsh`, `fish`, or `powershell`; with no SHELL, pdt finds the shell you use |
| `pdt completion SHELL --script` | print the completion script instead of installing it |

Inside an app folder, or any folder below it, leave `APP` off any command that takes one and pdt uses that app. For example, `pdt logs 3` in the `hello-world` folder reads run 3 of `hello-world`. Anywhere else, leave `APP` off or mistype it and pdt lists the apps in the project so you can pick one; a mistyped name also gets the closest app name. A mistyped command or option gets the closest one too, for example `pdt lgos` asks "Did you mean `pdt logs`?".

`pdt aws`, `pdt az`, and `pdt gcloud` hand your arguments straight to the cloud tool, and install it first if it is missing. For example, `pdt az account list` or `pdt aws sts get-caller-identity`. `pdt aws` runs AWS CLI version 2 through `uvx`.

`init`, `examples`, `completion`, `aws`, `az`, and `gcloud` work outside a project. Every other command needs a project.

After `pdt completion` installs tab completion, every new terminal window has it. pdt also prints one line that turns it on in the current window, for example `eval "$(pdt completion zsh --script)"`.
