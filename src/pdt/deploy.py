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
import subprocess
import time
from pathlib import Path

from pdt import __version__, config, console, powershell, pwsh, regions, runs_cli
from pdt.config import ConfigError
from pdt.deploy_common import CostEstimate, deployed_next_steps
from pdt.utils.email_auth import can_prompt
from pdt.utils.send_email import auth_env_file, email_problems, prepare_email_auth

PROVIDERS = {
    "google-cloud": "deploy_google_cloud.py",
    "aws": "deploy_aws.py",
    "azure": "deploy_azure.py",
    "windows": "deploy_windows.py",
}

# How many times pdt reads the run list, FOLLOW_SECONDS apart, for the run it just started.
NEW_RUN_TRIES = 6


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
    """Deploy one app; then, when it has no successful run yet, offer to start one.

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
    if app["schedule"] is None:
        problems.append("schedule is required to deploy")
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
    problem = regions.choose_region(app, provider, assume_yes)
    if problem != "":
        console.error(f"{console.value(app_name)}: {console.escape(problem)}")
        return 1
    if config.uses_email(app):
        prepare_email_auth(auth_env_file(app["dir"]))
    code = dispatch(provider, "deploy", app_name, assume_yes)
    # A declined plan exits nonzero, so 0 means the deploy completed.
    if code != 0:
        return code
    config.mark_deployed(app_name, True)
    run_code = first_run(app_name, provider, assume_yes, run)
    deployed_next_steps(app_name, started=run_code is not None)
    return run_code or 0


def first_run(app_name: str, provider: str, assume_yes: bool, run: bool | None) -> int | None:
    """Start a run of an app with no successful run yet and follow its log.

    Returns the exit code of `pdt logs --follow` (the run's result), or None
    when pdt started no run."""
    if run is False or (run is None and (assume_yes or not can_prompt(None))):
        return None
    history = run_history(provider, app_name)
    if history is None or any(item.status == "succeeded" for item in history):
        return None
    console.say()
    console.say(f"{console.value(app_name)} has "
                f"{'no successful run' if history else 'not run'} yet.")
    if not run:
        try:
            if not console.confirm("Start a run now and show its log?"):
                return None
        except EOFError:
            return None
    code = start(app_name)
    if code != 0:
        return code
    before = {item.id for item in history}
    for attempt in range(NEW_RUN_TRIES):
        if attempt > 0:
            time.sleep(runs_cli.FOLLOW_SECONDS)
        found = [item for item in run_history(provider, app_name) or [] if item.id not in before]
        if found:
            return dispatch(provider, "logs", app_name, False,
                            ["--", "--follow", "--id", found[0].id])
    console.note("the run list does not show the new run yet.")
    console.command(f"pdt logs {app_name} --follow", "print its lines as they arrive")
    return 0


def run_history(provider: str, app_name: str) -> list[runs_cli.Run] | None:
    """The app's runs from `pdt runs --json`, or None when they could not be read."""
    code, output = dispatch_output(provider, "runs", app_name, ["--", "--json"])
    return runs_cli.parse_runs(output) if code == 0 else None


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
    if not config.is_deployed(app_name):
        state = "paused" if paused else "running"
        console.say(f"{console.value(app_name)} is not deployed; the next pdt deploy will create "
                    f"it {state}.")
        return 0
    config.load_env(app["dir"])
    return dispatch(provider, "pause" if paused else "unpause", app_name, False)


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
    return dispatch(provider, "start", app_name, False)


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
    code = dispatch(provider, "destroy", app_name, assume_yes)
    if code == 0:
        config.mark_deployed(app_name, False)
    return code


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
