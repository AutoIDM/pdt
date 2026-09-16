# verify/

`verify/` is a pdt project that CI deploys to a real account on every provider, checks, and destroys. It is the one directory in this repo that holds a `pdt.yml`.

## Coverage

- `coverage.yml` is the source of truth for what verification covers. A behavior is covered only when its row names a scenario or an app.
- A change that adds a CLI command, a config key, a provider, or a lifecycle rule to pdt adds a row to `coverage.yml` and the scenario or app that exercises it, in the same change.
- `scripts/coverage.py --check` fails when a command in `src/pdt/cli.py`, a key in `config.APP_KEYS`, `PLATFORM_KEYS`, or `ENV_KEYS`, or a provider in `config.PROVIDERS` has no row, and when a row names a scenario or app that does not exist.
- The check runs in the `verify:check` CI job, in `tests/test_coverage.py`, and as a commit hook. Install the hook once with `uvx pre-commit install`.
- A row is `uncovered` only with a reason that says what CI cannot do. The uncovered rows in `coverage.yml` are the complete list; do not repeat them here.

## Scenarios

- A scenario is a function `(ctx) -> bool` in `scripts/verify.py` registered in `SCENARIOS`; `CLOUD_SCENARIOS` fixes the order a provider run uses, and `local` runs alone under `verify.py local`. Its name is what `coverage.yml` refers to.
- A scenario records steps with `ctx.record(name, problems)`, `ctx.check(name, checker)`, or `ctx.command(name, *pdt_args)`; an empty problem list is a PASS. It returns False at the first failed step.
- A scenario reads the account through `ctx.inventory()`, run records through `ctx.runs(app, since)`, and pdt through `ctx.run_pdt`. It never calls a provider CLI directly; add a listing or a run reader to `scripts/inventory.py` instead.
- The a app of a provider is `ctx.apps[0]` and the b app `ctx.apps[1]`; the run writes the b app's fire-time schedule into its `config.yml` and restores the file when the run ends.
- Every scenario's check logic gets a unit test in `tests/test_verify.py` against `FakeCloud`, with no network and no cloud account.
- A failure after the first deploy destroys every app before the run exits. Keep that order when adding steps.

## Matrix and templates

- The `apps:` list in `pdt.yml` is the matrix. Every provider keeps two apps, so a shared resource has a second owner while the first is destroyed.
- The app directories are generated from `scripts/templates/`. Edit a template, then run `uv run --with pyyaml python verify/scripts/sync_apps.py` from the repository root; `--check` reports drift and CI runs it.
- Never edit `verify/<app>/run.py` or `verify/<app>/config.yml` by hand.
- Never pin `pdt-cli` in `verify/.gitlab-ci.yml`. Each job installs the wheel the `build` job produced, so a run tests the commit it belongs to.
- Every run asserts the account is empty before the first deploy and after the last destroy. A run that starts on a dirty account fails instead of hiding the leftovers.
- The cloud jobs create and destroy real resources and cost real money. They are `interruptible: false`, because a cancelled run leaks what it made.
