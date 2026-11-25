# Deploying and operating

## Preview before changing anything (`--plan`)

Before creating or changing any cloud resources, preview the exact resource plan and monthly cost estimate with `--plan`:

```
pdt deploy my-report --plan
```

`pdt deploy APP --plan` asks no questions, creates or changes no cloud resources (except enabling free required Google Cloud APIs needed to read and price the project), and does not modify `pdt.yml` or `.pdt/state`.

## Deploy

```
pdt deploy my-report
```

A deploy to AWS, Azure, or Google Cloud builds the app's image with Docker on your computer when Docker is running, and in the cloud when it is not (see [Choosing where apps run](platforms.md)). The command prints a resource plan and a monthly cost estimate before it changes anything. Add `--yes` (or `-y`) to skip the question, or `--rebuild` to rebuild and push the image even when the app did not change.

The cost estimate shows prices in the currency of your computer's regional setting, or in US dollars when pdt cannot read it. pdt takes the price in that currency from the cloud when the cloud lists one. Otherwise it converts from US dollars at the cloud's own exchange rate, or at the European Central Bank's daily reference rate, and the estimate names the rate it used.

For a **scheduled job**, after each deploy pdt asks `Start a run now and show its log? [y/N]`. Typing `y` starts one run (as `pdt run my-report --deployed` does) and prints the run's log lines as they arrive (as `pdt logs my-report --follow` does). The deploy then exits with the run's result: `0` when the run succeeded, `1` when it failed. Because a run can have side effects—such as sending email—the default is no. With `--yes`, or with no terminal (a build server), pdt does not ask and starts no run. Add `--run` to start the run and follow its log with no question.

For a **web app** (`app_profile: web`), `pdt deploy` prints the deployed URL instead of asking to start a run, and restarts the service with the new code.

To deploy every enabled app in the project:

```
pdt deploy --all
pdt deploy --all --yes --skip-failures --run
```

`pdt deploy --all` does not offer a run per app; adding `--run` starts and follows a run of each deployed job one at a time after all deploys finish.

## Run, inspect, and manage secrets

To start one run of a deployed job now (or open a deployed web app in your browser), and read logs:

```
pdt run my-report --deployed
pdt run my-report --deployed --follow
pdt logs my-report
pdt runs my-report
pdt health
```

To compare or update a deployed app's secrets without running a full deploy:

```
pdt secrets my-report diff
pdt secrets my-report save
pdt secrets my-report get
echo "secret" | pdt secrets my-report set API_TOKEN
```

`diff` (the default) shows which `.env` keys differ from the deployed app, `save` uploads local `.env` values to the cloud secret store, `get` writes deployed values into `.env.<provider>`, and `set` updates one key from stdin.

To send deploy and run notifications to Slack or Microsoft Teams, run `pdt alerts setup` and `pdt alerts test` (see [Alerts](alerts.md)).

To sign in again, or to switch to a different cloud account:

```
pdt login my-report
```

To remove everything the deploy created (leaving stored files intact until you run `pdt storage my-report destroy`):

```
pdt destroy my-report
pdt destroy --all
```

## Pausing or turning off an app

An app that should stay deployed but not run for a while sets `pause: true`. For a scheduled job, the schedule stays in place and does not fire until `pause` goes back to `false` (you can still trigger a manual run with `pdt run APP --deployed`). For a web app, pausing stops its server. `pdt pause APP` and `pdt unpause APP` write the key into the app's own `pdt.yml` and apply it immediately; `pdt deploy` also honors it.

An app that is not ready sets `enabled: false` in its own `pdt.yml`. `pdt list` still shows it, and every other command (except `pdt destroy` while it is still deployed) acts as if the app is not there. `uv run run.py` in the app folder still runs it. `enabled: false` does not stop a schedule or web server that is already deployed, so run `pdt pause APP` or `pdt destroy APP` before you set it.

## Running pdt from CI

Every command reads its values from the environment it runs in. A build server sets them as CI variables, and no `.env` file is needed.

- A `.env` file is a convenience for a person working on their own machine. A value that is already in the environment wins over the same name in a `.env` file.
- `PDT_ENV_JSON` holds every value as one JSON object, for a CI system that keeps one secret instead of many. For example, `PDT_ENV_JSON={"PDT_TOKEN": "abc", "PDT_SMTP_USER": "reports@example.com"}`.
- pdt creates no `.env` file on a build server. It looks for the `CI` variable that build systems set, and for a missing terminal. Email authorization must therefore be done first: run `pdt run APP` once on a machine with a browser, then copy `PDT_SMTP_OAUTH_CACHE_B64` (or `PDT_GRAPH_MAIL_CACHE_B64` for Microsoft Graph) from your `.env` into the CI variables.
- `pdt deploy APP` and `pdt destroy APP` ask before they change anything. Add `--yes` (or `-y`) so they proceed without asking. On a build server, `pdt deploy APP` starts no run after the deploy; add `--run` to start a run and follow its log.

## Troubleshooting common issues

- **App already deployed to another provider, region, or account:** `.pdt/state` tracks where each app is currently deployed. If you change `platform:` in `pdt.yml`, `pdt deploy` asks whether to destroy the app at the old location first or cancel (in non-interactive mode it stops). Run `pdt destroy APP` first—it always targets the location recorded in `.pdt/state`—or run `pdt init` in the project root to move apps cleanly.
- **State lock held by a crashed run:** If a job dies mid-run while holding `state/lock`, the next run waits up to 30 minutes unless it can verify the holder is gone. Release it manually with `pdt storage APP unlock`.
- **Missing cloud permissions:** When your cloud account lacks a role or policy required to deploy, pdt prints the exact permission or policy to grant and stops without partial state.
- **Private git dependency fails in a cloud build:** A `Dockerfile` with `RUN --mount=type=ssh` requires your local SSH agent and therefore a local Docker build (`docker-build: local` with Docker running).
