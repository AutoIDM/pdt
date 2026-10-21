# Deploying

## Deploy

```
pdt deploy my-report
```

A deploy to AWS, Azure, or Google Cloud builds the app's image with Docker on your computer when Docker is running, and in the cloud when it is not (see [Choosing where jobs run](platforms.md)). The command prints a resource plan and a monthly cost estimate before it changes anything. Add `--yes` (or `-y`) to skip the question.

The cost estimate shows prices in the currency of your computer's regional setting, or in US dollars when pdt cannot read it. pdt takes the price in that currency from the cloud when the cloud lists one. Otherwise it converts from US dollars at the cloud's own exchange rate, or at the European Central Bank's daily reference rate, and the estimate names the rate it used.

After each deploy, pdt then asks `Start a run now and show its log? [y/N]`. A yes starts one run, as `pdt run my-report --deployed` does, and prints the run's log lines as they arrive, as `pdt logs my-report --follow` does. The deploy then exits with the run's result: 0 when the run succeeded, 1 when it failed. A run can have side effects, for example an email to real people, so the answer is no unless you type `y`. With `--yes`, or with no terminal (a build server), pdt does not ask and starts no run. Add `--run` to start the run and follow its log with no question. `pdt deploy --all` does not offer a run, so a deploy of many apps asks no question per app. `pdt deploy --all --run` deploys every app, then starts a run of each app that deployed and follows its log, one app at a time.

To start one run of the deployed job now, and then read its log:

```
pdt run my-report --deployed
pdt logs my-report
```

To remove everything the deploy created:

```
pdt destroy my-report
```

To sign in again, or to switch to a different cloud account:

```
pdt login my-report
```

### Pausing or turning off a job

An app that should stay deployed but not run for a while sets `pause: true`. The schedule stays in place and does not fire until the key goes back to `false`. `pdt pause APP` and `pdt unpause APP` write the key into the app's `config.yml` and apply it to the deployed schedule in the same step; `pdt deploy` also honors it. A paused app still runs when you ask for it with `pdt run APP --deployed`.

An app that is not ready sets `enabled: false` in its `config.yml`. `pdt list` still shows it, and every other command acts as if the app is not there. `uv run run.py` in the app folder still runs it. `enabled: false` does not stop a schedule that is already deployed, so run `pdt pause APP` or `pdt destroy APP` before you set it.

## Running pdt from CI

Every command reads its values from the environment it runs in. A build server sets them as CI variables, and no `.env` file is needed.

- A `.env` file is a convenience for a person working on their own machine. A value that is already in the environment wins over the same name in a `.env` file.
- `PDT_ENV_JSON` holds every value as one JSON object, for a CI system that keeps one secret instead of many. For example, `PDT_ENV_JSON={"PDT_TOKEN": "abc", "PDT_SMTP_USER": "reports@example.com"}`.
- pdt creates no `.env` file on a build server. It looks for the `CI` variable that build systems set, and for a missing terminal. Email authorization must therefore be done first: run `pdt run APP` once on a machine with a browser, then copy `PDT_SMTP_OAUTH_CACHE_B64` (or `PDT_GRAPH_MAIL_CACHE_B64` for Microsoft Graph) from your `.env` into the CI variables.
- `pdt deploy APP` and `pdt destroy APP` ask before they change anything. Add `--yes` (or `-y`) so they proceed without asking. On a build server, `pdt deploy APP` starts no run after the deploy; add `--run` to start a run and follow its log.
