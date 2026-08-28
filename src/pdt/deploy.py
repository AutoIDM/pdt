"""Provider-neutral deploy entry points.

Validates the app, then dispatches to one script per provider
(pdt/deploy_<provider>.py) with `uv run --script`, so each provider
installs its own SDK packages. Every provider script accepts
`deploy|destroy|login <app> [--yes] [--profile NAME]`.

PDT_PROJECT reaches the provider script through the environment, so the
child agrees with the parent about which project it is working on.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from pdt import config
from pdt.config import ConfigError
from pdt.utils.send_email import email_problems, prepare_email_auth

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
            f"provider {provider!r} is not supported yet "
            f"(supported: {', '.join(PROVIDERS)})")
    return app, provider


def dispatch(provider: str, command: str, app_name: str, assume_yes: bool,
             profile: str | None = None) -> int:
    script = Path(__file__).with_name(PROVIDERS[provider])
    args = ["uv", "run", "--script", str(script), command, app_name]
    if assume_yes:
        args.append("--yes")
    if profile:
        args += ["--profile", profile]
    env = dict(os.environ, PDT_PROJECT=str(config.find_project()))
    return subprocess.run(args, check=False, env=env).returncode


def deploy(app_name: str, assume_yes: bool = False, profile: str | None = None) -> int:
    try:
        app, provider = _load(app_name)
    except ConfigError as e:
        print(f"error: {e}")
        return 1
    problems = config.validate_app(app_name)
    env_files = config.load_env(app["dir"])
    for problem in config.check_env(app["env"]):
        problems.append(f"env: {problem}")
    if app["schedule"] is None:
        problems.append("schedule is required to deploy")
    if config.uses_email(app):
        problems.extend(email_problems(app["config"], check_oauth=False))
    if problems:
        for problem in problems:
            print(f"error: {app_name}: {problem}")
        return 1
    if config.uses_email(app):
        env_file = env_files[0] if env_files else app["dir"] / ".env"
        prepare_email_auth(env_file)
    return dispatch(provider, "deploy", app_name, assume_yes, profile)


def login(app_name: str, profile: str | None = None) -> int:
    try:
        app, provider = _load(app_name)
    except ConfigError as e:
        print(f"error: {e}")
        return 1
    config.load_env(app["dir"])
    return dispatch(provider, "login", app_name, False, profile)


def destroy(app_name: str, assume_yes: bool = False, profile: str | None = None) -> int:
    try:
        app, provider = _load(app_name)
    except ConfigError as e:
        print(f"error: {e}")
        return 1
    config.load_env(app["dir"])
    return dispatch(provider, "destroy", app_name, assume_yes, profile)


def confirm(actions: list[str], assume_yes: bool,
            cost_lines: list[str] | None = None) -> bool:
    print("Plan:")
    for action in actions:
        print(f"  {action}")
    for line in cost_lines or []:
        print(line)
    if assume_yes:
        return True
    answer = input("Proceed? [y/N] ").strip().lower()
    return answer in ("y", "yes")
