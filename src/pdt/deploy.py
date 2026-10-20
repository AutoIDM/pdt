"""Provider-neutral deploy entry points.

Validates the app, then dispatches to one script per provider
(pdt/deploy_<provider>.py) with `uv run --script`, so each provider
installs its own SDK packages. Every provider script accepts
`deploy|destroy|login <app> [--yes]`, and also
`pause|unpause|start <app>`,
`storage <app> -- <ls|get|query|unlock|destroy> [args...]` and
`runs|logs <app> -- [args...]`. The `--` keeps flags such as `--json` for
`pdt.runs_cli`, which parses them.

PDT_PROJECT reaches the provider script through the environment, so the
child agrees with the parent about which project it is working on.
PDT_CLI_VERSION does the same for pdt's version: from a clone, the script
imports pdt from src/, where no package metadata says which version it is,
and the image a PowerShell app builds pins pdt-cli at that version.
PDT_JSON_OUTPUT tells a script given `--json` to print every line but the
JSON on stderr; see `pdt.console.json_output`.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import nullcontext
from pathlib import Path

from pdt import (__version__, alerts_cli, config, console, lifecycle, powershell, pwsh, regions,
                 runs_cli)
from pdt.config import ConfigError
from pdt.deploy_common import REBUILD_ENV, CostEstimate
from pdt.utils.email_auth import can_prompt
from pdt.utils.send_email import auth_env_file, email_problems, prepare_email_auth

PROVIDERS = {
    "google-cloud": "deploy_google_cloud.py",
    "aws": "deploy_aws.py",
    "azure": "deploy_azure.py",
    "windows": "deploy_windows.py",
}


def _load(app_name: str):
    app = config.merged_app(app_name)
    provider = app["platform"].get("provider")
    if provider not in PROVIDERS:
        raise ConfigError(
            f"platform.provider must be one of: {', '.join(PROVIDERS)}. "
            f"Set it under platform: in {config.PROJECT_FILE}, or in {app_name}/{config.APP_FILE}.")
    return app, provider


# A provider script whose packages take minutes to install on its first run,
# and the name of what they are.
SLOW_INSTALLS = {"deploy_azure.py": "the Azure CLI"}


def announce_install(script: Path) -> None:
    """Say so before uv installs a slow provider script's packages for the first time."""
    tool = SLOW_INSTALLS.get(script.name)
    if tool is None:
        return
    ready = subprocess.run(["uv", "sync", "--script", str(script), "--offline", "--check"],
                           capture_output=True).returncode == 0
    if not ready:
        console.status(f"Installing {tool}. This happens once and can take several minutes...")


def provider_command(provider: str, command: str, app_name: str, assume_yes: bool,
                     extra: list[str] | None = None) -> list[str]:
    script = Path(__file__).with_name(PROVIDERS[provider])
    announce_install(script)
    # When someone runs pdt with `uvx`, uv installs pdt inside its own cache
    # folder, so this script is in the cache too. uv refuses to run a script
    # from its cache, because it treats the script's folder as the project.
    # --project names the current folder instead. The script still installs
    # only the packages listed at its top.
    args = ["uv", "run", "--project", str(Path.cwd()), "--script", str(script),
            command, app_name]
    if assume_yes:
        args.append("--yes")
    return args + (extra or [])


def provider_env(extra: list[str] | None = None, rebuild: bool = False) -> dict[str, str]:
    env = dict(os.environ, PDT_PROJECT=str(config.find_project()), PDT_CLI_VERSION=__version__)
    if "--json" in (extra or []):
        env["PDT_JSON_OUTPUT"] = "1"
    if rebuild:
        env[REBUILD_ENV] = "1"
    return env


def dispatch(provider: str, command: str, app_name: str, assume_yes: bool,
             extra: list[str] | None = None, rebuild: bool = False) -> int:
    args = provider_command(provider, command, app_name, assume_yes, extra)
    return subprocess.run(args, check=False, env=provider_env(extra, rebuild)).returncode


def dispatch_output(provider: str, command: str, app_name: str,
                    extra: list[str], stderr=None) -> tuple[int, str]:
    """Like dispatch, but return what the provider script prints on stdout. `stderr` is a file
    for the script's other lines, or None for this process's stderr."""
    proc = subprocess.run(provider_command(provider, command, app_name, False, extra),
                          check=False, env=provider_env(extra), stdout=subprocess.PIPE,
                          stderr=stderr, text=True)
    return proc.returncode, proc.stdout


def deploy(app_name: str, assume_yes: bool = False, run: bool | None = None,
           rebuild: bool = False) -> int:
    """Deploy one app; then offer to start a run of it.

    `run` None asks a person at a terminal and starts nothing with `--yes` or
    with no terminal, True starts the run with no question (`--run`), and
    False never offers it (`pdt deploy --all`). The exit code is the deploy's,
    or the run's when pdt followed one."""
    try:
        app, provider = _load(app_name)
    except ConfigError as e:
        console.error(console.escape(str(e)))
        return 1
    problems = config.validate_app(app_name)
    sources = config.env_file_lines(app["dir"])
    config.load_env(app["dir"])
    missing = config.missing_env(app)
    if missing != "":
        problems.append(missing)
    if config.uses_email(app):
        problems.extend(email_problems(app["config"], check_oauth=False))
    lines = [f"runs {console.value('run.py')}"]
    if config.powershell_scripts(app["dir"]):
        try:
            scan = powershell.scan(app, provider)
            problems.extend(powershell.report(scan)[0])
            lines = powershell.summary_lines(scan)
        except (pwsh.PwshError, powershell.PowerShellError) as e:
            problems.append(str(e))
    if problems:
        for problem in problems:
            console.error(f"{console.value(app_name)}: {console.escape(problem)}")
        return 1
    console.name(app_name)
    for line in [*lines, *config.found_env_lines(app), *sources]:
        console.detail(line)
    problem = choose_schedule(app, assume_yes) or regions.choose_region(app, provider, assume_yes)
    if problem != "":
        console.error(f"{console.value(app_name)}: {console.escape(problem)}")
        return 1
    if config.uses_email(app):
        prepare_email_auth(auth_env_file(app["dir"]))
    alerts_cli.offer(app_name, assume_yes)
    code = provider_step("deploy", provider, app_name, assume_yes, rebuild)
    if code != 0:
        return code
    run_code = offer_run(app_name, provider, assume_yes, run)
    deployed_next_steps(app_name, started=run_code is not None)
    return run_code or 0


# The schedule a deploy suggests for an app that sets none.
DEFAULT_SCHEDULE = "daily"


def choose_schedule(app: dict, assume_yes: bool) -> str:
    """When the app has no schedule, ask for one and save it in the app's config.yml.

    Creates config.yml when the app folder has none. Returns a problem for the
    user, or "". --yes takes DEFAULT_SCHEDULE, the same way it takes every
    other default; a run with no one to ask and no --yes stops with the line
    to add.
    """
    if app["schedule"] is not None:
        return ""
    name = app["name"]
    where = f"{name}/{config.APP_FILE}"
    words = config.describe_schedule(DEFAULT_SCHEDULE)
    schedule = DEFAULT_SCHEDULE
    if assume_yes:
        console.say(f"{console.value(name)} has no schedule, so it runs "
                    f"{console.value(DEFAULT_SCHEDULE)} ({console.escape(words)}).")
    elif not can_prompt(None):
        return (f"no schedule is set. Add `schedule: {DEFAULT_SCHEDULE}` to {where}, or run "
                f"again with --yes to run it {DEFAULT_SCHEDULE} ({words}).")
    else:
        console.say()
        console.say(f"{console.value(name)} has no schedule yet. Answer hourly, daily, weekly, "
                    f"monthly, or a cron expression such as {console.value('30 6 * * 1-5')}.")
        while True:
            try:
                schedule = console.ask(f"How often should {name} run?",
                                       DEFAULT_SCHEDULE) or DEFAULT_SCHEDULE
            except EOFError:
                return f"no schedule is set. Add `schedule: {DEFAULT_SCHEDULE}` to {where}."
            try:
                words = config.describe_schedule(schedule)
                break
            except ConfigError as e:
                console.error(console.escape(str(e)))
    saved = config.save_app_key(app, "schedule", schedule)
    app["schedule"] = schedule
    console.done(f"Saved schedule {console.value(schedule)} ({console.escape(words)}) to "
                 f"{console.value(saved.relative_to(config.find_project()))}.")
    return ""


# The started, completed, and failed events of each provider_step command.
STEP_EVENTS = {
    "deploy": (lifecycle.APP_DEPLOY_STARTED, lifecycle.APP_DEPLOY_COMPLETED,
               lifecycle.APP_DEPLOY_FAILED),
    "destroy": (lifecycle.APP_DESTROY_STARTED, lifecycle.APP_DESTROY_COMPLETED,
                lifecycle.APP_DESTROY_FAILED),
}


def provider_step(command: str, provider: str, app_name: str, assume_yes: bool,
                  rebuild: bool = False) -> int:
    """Run `deploy` or `destroy` through the provider script between its STEP_EVENTS, and
    on success record whether the app is now deployed."""
    started, completed, failed = STEP_EVENTS[command]
    params = lifecycle.app_params(app_name, provider)
    lifecycle.emit(started, **params)
    try:
        code = dispatch(provider, command, app_name, assume_yes, rebuild=rebuild)
    except KeyboardInterrupt:
        lifecycle.emit(failed, **params, reason="interrupted")
        raise
    if code in (DECLINED, 130, -signal.SIGINT):
        lifecycle.emit(failed, **params, exit_code=code,
                       reason="declined" if code == DECLINED else "interrupted")
        return code
    if code != 0:
        lifecycle.emit(failed, **params, exit_code=code)
        return code
    config.mark_deployed(app_name, command == "deploy")
    lifecycle.emit(completed, **params)
    return 0


def offer_run(app_name: str, provider: str, assume_yes: bool, run: bool | None) -> int | None:
    """Offer to start a run of the app just deployed and follow its log.

    Returns the run's result, or None when pdt started no run."""
    if run is False or (run is None and (assume_yes or not can_prompt(None))):
        return None
    console.say()
    if not run:
        try:
            if not console.confirm("Start a run now and show its log?"):
                return None
        except EOFError:
            return None
    console.status(f"Starting a run of {console.value(app_name)}...")
    return dispatch(provider, "start", app_name, False, ["--", "--follow"])


def start_run(app_name: str) -> int:
    """Start a run of a deployed app and follow its log; the run's exit code."""
    try:
        _app, provider = _load(app_name)
    except ConfigError as e:
        console.error(console.escape(str(e)))
        return 1
    return offer_run(app_name, provider, False, True) or 0


def run_history(provider: str, app_name: str, stderr=None) -> list[runs_cli.Run] | None:
    """The app's runs from `pdt runs --json`, or None when they could not be read."""
    code, output = dispatch_output(provider, "runs", app_name, ["--", "--json"], stderr)
    return runs_cli.parse_runs(output) if code == 0 else None


def deployed_next_steps(app_name: str, started: bool = False) -> None:
    """The same commands after every provider's deploy; no `pdt run` when pdt just started one."""
    console.say()
    console.next_steps([
        *([] if started else [(f"pdt run {app_name} --deployed", "start a run now")]),
        (f"pdt logs {app_name}", "read the log of the newest run"),
        (f"pdt runs {app_name}", "list the recent runs"),
        (f"pdt health {app_name}", "show whether the last run succeeded"),
    ])


def secrets(app_name: str, action: str, assume_yes: bool = False,
            name: str | None = None) -> int:
    try:
        app, provider = _load(app_name)
    except ConfigError as e:
        console.error(console.escape(str(e)))
        return 1
    config.load_env(app["dir"])
    if action in ("diff", "save"):
        missing = config.missing_env(app)
        if missing != "":
            console.error(f"{console.value(app_name)}: {console.escape(missing)}")
            return 1
    return dispatch(provider, "secrets", app_name, assume_yes, [action, *([name] if name else [])])


def login(app_name: str) -> int:
    try:
        app, provider = _load(app_name)
    except ConfigError as e:
        console.error(console.escape(str(e)))
        return 1
    config.load_env(app["dir"])
    return dispatch(provider, "login", app_name, False)


def storage(app_name: str, rest: list[str]) -> int:
    try:
        app, provider = _load(app_name)
    except ConfigError as e:
        console.error(console.escape(str(e)))
        return 1
    config.load_env(app["dir"])
    return dispatch(provider, "storage", app_name, False, ["--", *rest])


def runs(app_name: str, rest: list[str]) -> int:
    try:
        app, provider = _load(app_name)
    except ConfigError as e:
        console.error(console.escape(str(e)))
        return 1
    config.load_env(app["dir"])
    return dispatch(provider, "runs", app_name, False, ["--", *rest])


def logs(app_name: str, rest: list[str]) -> int:
    try:
        app, provider = _load(app_name)
    except ConfigError as e:
        console.error(console.escape(str(e)))
        return 1
    config.load_env(app["dir"])
    return dispatch(provider, "logs", app_name, False, ["--", *rest])


def pause(app_name: str, paused: bool) -> int:
    """Write `pause:` into the app's config, then apply it to the deployed schedule."""
    try:
        app, provider = _load(app_name)
    except ConfigError as e:
        console.error(console.escape(str(e)))
        return 1
    path = config.save_app_key(app, "pause", paused)
    setting = f"pause: {'true' if paused else 'false'}"
    console.done(f"{console.value(app_name)}: {console.value(setting)} saved in "
                 f"{console.value(path)}")
    deployed = config.is_deployed(app_name)
    if deployed:
        config.load_env(app["dir"])
        code = dispatch(provider, "pause" if paused else "unpause", app_name, False)
    else:
        state = "paused" if paused else "running"
        console.say(f"{console.value(app_name)} is not deployed; the next pdt deploy will create "
                    f"it {state}.")
        code = 0
    if code == 0:
        lifecycle.emit(lifecycle.JOB_PAUSED if paused else lifecycle.JOB_UNPAUSED,
                       **lifecycle.app_params(app_name, provider), deployed=deployed)
    return code


def start(app_name: str, follow: bool = False) -> int:
    """Start one run of the deployed job now; with `follow`, print its log until it ends and
    return the run's exit code."""
    try:
        app, provider = _load(app_name)
    except ConfigError as e:
        console.error(console.escape(str(e)))
        return 1
    if not config.is_deployed(app_name):
        console.error(f"{console.value(app_name)} is not deployed; "
                      f"run {console.value(f'pdt deploy {app_name}')} first")
        return 1
    config.load_env(app["dir"])
    if follow:
        return dispatch(provider, "start", app_name, False, ["--", "--follow"])
    code = dispatch(provider, "start", app_name, False)
    if code == 0:
        console.say()
        console.next_steps([
            (f"pdt logs {app_name} --follow", "print the log lines as they arrive"),
            (f"pdt runs {app_name}", "list the recent runs"),
            (f"pdt health {app_name}", "show whether the last run succeeded"),
        ])
    return code


# At most this many provider scripts read run histories at once in `pdt health`. With 16 apps,
# 8 took 7.1 s (median of 3), 16 took 6.1 s, and 4 took 11.9 s; each script holds about
# 110 MB, so 8 keeps the total under 1 GB.
HEALTH_PARALLEL = 8


def child_can_ask() -> bool:
    """Whether a provider script started for --json output could ask a question here: it asks
    on this process's stderr (see console.json_output), not on stdout as can_prompt checks."""
    return os.environ.get("CI", "").strip() == "" and sys.stdin.isatty() and sys.stderr.isatty()


def health(app_names: list[str], as_json: bool) -> int:
    """Read the runs of every app, HEALTH_PARALLEL provider scripts at once, then print the table.

    A script that runs beside others writes its lines to a file, which prints afterwards in app
    order. It has no terminal, so it cannot ask for a login. When a person could answer, each app
    whose read failed runs again alone, as it did before, so its provider can ask."""
    providers, errors = {}, {}
    for app_name in app_names:
        try:
            providers[app_name] = _load(app_name)[1]
        except ConfigError as e:
            errors[app_name] = str(e)
    found, lines = {}, {}

    def read(app_name: str) -> None:
        # The provider script loads the app's .env itself, so one app's env
        # never leaks into the next app's run.
        with tempfile.TemporaryFile("w+") as stderr:
            found[app_name] = run_history(providers[app_name], app_name, stderr)
            stderr.seek(0)
            lines[app_name] = stderr.read()

    message = f"checking {len(providers)} apps"
    with nullcontext(console.Wait(message)) if as_json else console.waiting(message) as wait:
        with ThreadPoolExecutor(HEALTH_PARALLEL) as pool:
            futures = [pool.submit(read, app_name) for app_name in providers]
            for done, future in enumerate(as_completed(futures), 1):
                future.result()
                wait.now(f"{message}: {done} done")
    can_ask = child_can_ask()
    app_runs = {}
    for app_name in app_names:
        if app_name in errors:
            console.error(console.escape(errors[app_name]))
            app_runs[app_name] = None
        elif found[app_name] is None and can_ask:
            app_runs[app_name] = run_history(providers[app_name], app_name)
        else:
            sys.stderr.write(lines[app_name])
            app_runs[app_name] = found[app_name]
    return runs_cli.health(app_runs, as_json)


def destroy(app_name: str, assume_yes: bool = False) -> int:
    try:
        app, provider = _load(app_name)
    except ConfigError as e:
        console.error(console.escape(str(e)))
        return 1
    config.load_env(app["dir"])
    return provider_step("destroy", provider, app_name, assume_yes)


# The exit code of a provider script whose plan the user declined, so provider_step can
# give a declined deploy or destroy reason=declined.
DECLINED = 10


# The exit code of a provider script whose plan the user declined, so `pdt deploy --all`
# and `pdt destroy --all` can tell a cancelled app from a failed one.
DECLINED = 10


def confirm(actions: list[str], assume_yes: bool,
            cost: CostEstimate | None = None) -> bool:
    console.heading("Plan:")
    for action in actions:
        console.bullet(action if isinstance(action, console.Markup) else console.escape(action))
    if cost is not None:
        cost.show()
    return proceed(assume_yes)


def proceed(assume_yes: bool) -> bool:
    if assume_yes:
        return True
    if not can_prompt(None):
        console.say()
        console.warn("there is no one to answer. Run this in a terminal, "
                     "or add --yes to proceed without asking.")
        return False
    try:
        return console.confirm()
    except EOFError:
        return False
