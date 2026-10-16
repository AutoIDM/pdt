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
from pathlib import Path

from pdt import (__version__, alerts_cli, config, console, lifecycle, powershell, pwsh, regions,
                 runs_cli)
from pdt.config import ConfigError
from pdt.deploy_common import CostEstimate
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


def provider_env(extra: list[str] | None = None) -> dict[str, str]:
    env = dict(os.environ, PDT_PROJECT=str(config.find_project()), PDT_CLI_VERSION=__version__)
    if "--json" in (extra or []):
        env["PDT_JSON_OUTPUT"] = "1"
    return env


def dispatch(provider: str, command: str, app_name: str, assume_yes: bool,
             extra: list[str] | None = None) -> int:
    args = provider_command(provider, command, app_name, assume_yes, extra)
    return subprocess.run(args, check=False, env=provider_env(extra)).returncode


def dispatch_output(provider: str, command: str, app_name: str,
                    extra: list[str]) -> tuple[int, str]:
    """Like dispatch, but return what the provider script prints on stdout."""
    proc = subprocess.run(provider_command(provider, command, app_name, False, extra),
                          check=False, env=provider_env(extra), stdout=subprocess.PIPE,
                          text=True)
    return proc.returncode, proc.stdout


def deploy(app_name: str, assume_yes: bool = False, run: bool | None = None) -> int:
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
    code = provider_step("deploy", provider, app_name, assume_yes)
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


def provider_step(command: str, provider: str, app_name: str, assume_yes: bool) -> int:
    """Run `deploy` or `destroy` through the provider script between its STEP_EVENTS, and
    on success record whether the app is now deployed."""
    started, completed, failed = STEP_EVENTS[command]
    params = lifecycle.app_params(app_name, provider)
    lifecycle.emit(started, **params)
    try:
        code = dispatch(provider, command, app_name, assume_yes)
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


def run_history(provider: str, app_name: str) -> list[runs_cli.Run] | None:
    """The app's runs from `pdt runs --json`, or None when they could not be read."""
    code, output = dispatch_output(provider, "runs", app_name, ["--", "--json"])
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


def start(app_name: str) -> int:
    """Start one run of the deployed job now."""
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
    code = dispatch(provider, "start", app_name, False)
    if code == 0:
        console.say()
        console.next_steps([
            (f"pdt logs {app_name} --follow", "print the log lines as they arrive"),
            (f"pdt runs {app_name}", "list the recent runs"),
            (f"pdt health {app_name}", "show whether the last run succeeded"),
        ])
    return code


def health(app_names: list[str], as_json: bool) -> int:
    app_runs = {}
    for app_name in app_names:
        try:
            _app, provider = _load(app_name)
        except ConfigError as e:
            console.error(console.escape(str(e)))
            app_runs[app_name] = None
            continue
        # The provider script loads the app's .env itself, so one app's env
        # never leaks into the next app's run.
        app_runs[app_name] = run_history(provider, app_name)
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
