"""Provider-neutral deploy entry points.

Validates the app, then dispatches to one script per provider
(pdt/deploy_<provider>.py) with `uv run --script`, so each provider
installs its own SDK packages. Every provider script accepts
`deploy|destroy|login <app> [--yes]`, and also
`storage <app> <ls|get|query|destroy> [args...]` and
`runs|logs <app> -- [args...]`. The `--` keeps flags such as `--json` for
`pdt.runs_cli`, which parses them.

PDT_PROJECT reaches the provider script through the environment, so the
child agrees with the parent about which project it is working on.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from pdt import config, console, runs_cli
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


def provider_command(provider: str, command: str, app_name: str, assume_yes: bool,
                     extra: list[str] | None = None) -> list[str]:
    script = Path(__file__).with_name(PROVIDERS[provider])
    args = ["uv", "run", "--script", str(script), command, app_name]
    if assume_yes:
        args.append("--yes")
    return args + (extra or [])


def dispatch(provider: str, command: str, app_name: str, assume_yes: bool,
             extra: list[str] | None = None) -> int:
    env = dict(os.environ, PDT_PROJECT=str(config.find_project()))
    args = provider_command(provider, command, app_name, assume_yes, extra)
    return subprocess.run(args, check=False, env=env).returncode


def dispatch_output(provider: str, command: str, app_name: str,
                    extra: list[str]) -> tuple[int, str]:
    """Like dispatch, but return the provider script's output instead of showing it."""
    env = dict(os.environ, PDT_PROJECT=str(config.find_project()))
    proc = subprocess.run(provider_command(provider, command, app_name, False, extra),
                          check=False, env=env, stdout=subprocess.PIPE, text=True)
    return proc.returncode, proc.stdout


def deploy(app_name: str, assume_yes: bool = False) -> int:
    try:
        app, provider = _load(app_name)
    except ConfigError as e:
        console.error(str(e))
        return 1
    problems = config.validate_app(app_name)
    config.load_env(app["dir"])
    for problem in config.check_env(app["env"]):
        problems.append(f"env: {problem}")
    if app["schedule"] is None:
        problems.append("schedule is required to deploy")
    if config.uses_email(app):
        problems.extend(email_problems(app["config"], check_oauth=False))
    if problems:
        for problem in problems:
            console.error(f"{app_name}: {problem}")
        return 1
    if config.uses_email(app):
        prepare_email_auth(auth_env_file(app["dir"]))
    code = dispatch(provider, "deploy", app_name, assume_yes)
    if code == 0:
        # A declined plan exits nonzero, so 0 means the deploy completed.
        config.mark_deployed(app_name, True)
    return code


def secrets(app_name: str, action: str, assume_yes: bool = False,
            name: str | None = None) -> int:
    try:
        app, provider = _load(app_name)
    except ConfigError as e:
        console.error(str(e))
        return 1
    config.load_env(app["dir"])
    if action in ("diff", "save"):
        problems = config.check_env(app["env"])
        if problems:
            for problem in problems:
                console.error(f"{app_name}: env: {problem}")
            return 1
    return dispatch(provider, "secrets", app_name, assume_yes, [action, *([name] if name else [])])


def login(app_name: str) -> int:
    try:
        app, provider = _load(app_name)
    except ConfigError as e:
        console.error(str(e))
        return 1
    config.load_env(app["dir"])
    return dispatch(provider, "login", app_name, False)


def storage(app_name: str, rest: list[str]) -> int:
    try:
        app, provider = _load(app_name)
    except ConfigError as e:
        console.error(str(e))
        return 1
    config.load_env(app["dir"])
    return dispatch(provider, "storage", app_name, False, rest)


def runs(app_name: str, rest: list[str]) -> int:
    try:
        app, provider = _load(app_name)
    except ConfigError as e:
        console.error(str(e))
        return 1
    config.load_env(app["dir"])
    return dispatch(provider, "runs", app_name, False, ["--", *rest])


def logs(app_name: str, rest: list[str]) -> int:
    try:
        app, provider = _load(app_name)
    except ConfigError as e:
        console.error(str(e))
        return 1
    config.load_env(app["dir"])
    return dispatch(provider, "logs", app_name, False, ["--", *rest])


def health(app_names: list[str], as_json: bool) -> int:
    app_runs = {}
    for app_name in app_names:
        try:
            _app, provider = _load(app_name)
        except ConfigError as e:
            console.error(str(e))
            app_runs[app_name] = None
            continue
        # The provider script loads the app's .env itself, so one app's env
        # never leaks into the next app's run.
        code, output = dispatch_output(provider, "runs", app_name, ["--", "--json"])
        app_runs[app_name] = runs_cli.parse_runs(output) if code == 0 else None
        if app_runs[app_name] is None:
            console.say(output.rstrip())
    return runs_cli.health(app_runs, as_json)


def destroy(app_name: str, assume_yes: bool = False) -> int:
    try:
        app, provider = _load(app_name)
    except ConfigError as e:
        console.error(str(e))
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
        console.bullet(action)
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
