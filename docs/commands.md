# Commands

Back to the [README](../README.md).

| Command | What it does |
| --- | --- |
| `pdt init [DIR]` | create a project here, or in DIR |
| `pdt examples` | list the example apps bundled with pdt |
| `pdt new APP --from EXAMPLE` | add an app to the project |
| `pdt list` | show every app with its schedule and provider; `--names` prints only the enabled app names |
| `pdt validate` | check the config files and the required env vars |
| `pdt run APP` | run an app on this machine |
| `pdt deploy APP` | deploy an app to its configured platform |
| `pdt deploy --all` | deploy every enabled app, in order; asks whether to skip and disable an app that fails; add `--yes --skip-failures` to run unattended |
| `pdt destroy APP` | remove everything deploy created |
| `pdt secrets APP` | show which `.env` values differ from the deployed app |
| `pdt secrets APP save` | send your `.env` values to the deployed app; the next run uses them |
| `pdt secrets APP get` | copy the deployed values into a `.env.<provider>` file |
| `pdt secrets APP set NAME` | put one value, read from stdin, into the deployed app's secrets |
| `pdt login APP` | sign in again to the app's cloud provider |
| `pdt storage APP ls|get|query|destroy` | look at, fetch, query, or delete the app's stored files |
| `pdt runs APP` | list the deployed app's runs with each run's exit code: the 10 newest, or with `--since 3d` (or `12h`, `2w`, `2026-09-20`, `2026-09-20T14:00`) every run since then, `--span 1d` keeping only the runs within that long after `--since` and `--count N` keeping only the N newest; a run's number is its place among every run pdt can still find, so it stays the same whichever runs print |
| `pdt logs APP [N]` | read the log of run N as `pdt runs` numbers it; with no N, the newest run, and `--failed` picks the newest failed run instead; `--since`, `--span`, and `--count` limit which runs those two choose from, `--errors` leaves out DEBUG and INFO lines; the last 20 lines print, `--lines N` prints N instead, `--head` prints the first lines instead of the last, and `--full` prints every line |
| `pdt health [APP]` | show whether each app's last run succeeded; exits 1 when one failed |
| `pdt az ...` | run the Azure CLI that pdt installs |
| `pdt gcloud ...` | run the Google Cloud CLI that pdt installs |
| `pdt completion [SHELL]` | turn on tab completion for a shell |

Leave `APP` off `run`, `deploy`, `destroy`, `secrets`, or `login`, or mistype it, and pdt lists the apps in the project so you can pick one.

`pdt az` and `pdt gcloud` hand your arguments straight to the cloud tool, and install it first if it is missing. For example, `pdt az account list`.
