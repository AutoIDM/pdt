"""Provider-neutral deploy entry points.

Validates the app, then dispatches to one script per provider
(pdt/deploy_<provider>.py) with `uv run --script`, so each provider
installs its own SDK packages. Every provider script accepts
`deploy|destroy|login <app> [--yes] [--profile NAME]`, and also
`storage <app> <ls|get|query|destroy> [args...]`.

PDT_PROJECT reaches the provider script through the environment, so the
child agrees with the parent about which project it is working on.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from pdt import config, console
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


def recorded_platform(app_name: str) -> dict:
    path = config.find_project() / ".pdt" / "terraform" / "deployments" / (app_name + ".json")
    if not path.is_file():
        return {}
    try:
        record = json.loads(path.read_text())
        platform = {key: value for key, value in record["identity"].items() if key in config.PLATFORM_KEYS}
        platform["provider"] = record["provider"]
        return platform
    except (ValueError, KeyError, TypeError) as exc:
        raise ConfigError(f"{path}: invalid deployment record; restore this file from its backup") from exc


def _load(app_name: str, deployed: bool = False):
    if Path(app_name).name != app_name or app_name in (".", ".."):
        raise ConfigError("the app name must be one folder name")
    app = config.merged_app(app_name)
    if deployed:
        app["platform"].update(recorded_platform(app_name))
    provider = app["platform"].get("provider")
    if provider not in PROVIDERS:
        raise ConfigError(
            f"platform.provider must be one of: {', '.join(PROVIDERS)}. "
            f"Set it under platform: in {config.PROJECT_FILE}, or in {app_name}/{config.APP_FILE}.")
    return app, provider


def dispatch(provider: str, command: str, app_name: str, assume_yes: bool,
             profile: str | None = None, extra: list[str] | None = None) -> int:
    script = Path(__file__).with_name(PROVIDERS[provider])
    args = ["uv", "run", "--script", str(script), command, app_name]
    if assume_yes:
        args.append("--yes")
    if profile:
        args += ["--profile", profile]
    args += extra or []
    env = dict(os.environ, PDT_PROJECT=str(config.find_project()))
    if command == "destroy":
        recorded = recorded_platform(app_name)
        if recorded:
            env["PDT_DEPLOYMENT_APP"] = app_name
            env["PDT_DEPLOYMENT_PLATFORM"] = json.dumps(recorded)
            path = config.find_project() / ".pdt" / "terraform" / "deployments" / (app_name + ".json")
            record = json.loads(path.read_text())
            env["PDT_DEPLOYMENT_CONTEXT"] = json.dumps({key: record[key] for key in ("schedule", "timezone") if key in record})
    return subprocess.run(args, check=False, env=env).returncode


def deploy(app_name: str, assume_yes: bool = False, profile: str | None = None) -> int:
    try:
        app, provider = _load(app_name)
    except ConfigError as e:
        console.error(str(e))
        return 1
    recorded = recorded_platform(app_name)
    defaults = {"aws": "fargate", "azure": "container_apps", "google-cloud": "cloud-run-job", "windows": "task_scheduler"}
    for key, previous in recorded.items():
        current = app["platform"].get(key)
        if key == "runtime" and current is None:
            current = defaults.get(provider)
        if current is not None and current != previous:
            console.error(f"{app_name}/config.yml: platform.{key} differs from the deployed value; "
                          f"run pdt destroy {app_name} before moving it")
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
    code = dispatch(provider, "deploy", app_name, assume_yes, profile)
    if code == 0:
        # A declined plan exits nonzero, so 0 means the deploy completed.
        config.mark_deployed(app_name, True)
    return code


def login(app_name: str, profile: str | None = None) -> int:
    try:
        app, provider = _load(app_name)
    except ConfigError as e:
        console.error(str(e))
        return 1
    config.load_env(app["dir"])
    return dispatch(provider, "login", app_name, False, profile)


def storage(app_name: str, rest: list[str], profile: str | None = None) -> int:
    try:
        app, provider = _load(app_name)
    except ConfigError as e:
        console.error(str(e))
        return 1
    config.load_env(app["dir"])
    return dispatch(provider, "storage", app_name, False, profile, rest)


def destroy(app_name: str, assume_yes: bool = False, profile: str | None = None) -> int:
    try:
        app, provider = _load(app_name, deployed=True)
    except ConfigError as e:
        console.error(str(e))
        return 1
    config.load_env(app["dir"])
    code = dispatch(provider, "destroy", app_name, assume_yes, profile)
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
