"""Load, merge, and validate pdt configuration.

A project is a directory holding pdt.yml. An app is a directory inside it
that contains run.py. Commands find the project by walking up from the
working directory, so pdt works the same whether it was installed from
PyPI or run from a clone of this repository.

Merge order for one app, least to most specific:
  1. `platform:` defaults in the project pdt.yml
  2. the app's entry under `apps:` in pdt.yml
  3. the app's own config.yml
  4. PDT_<APP>_<KEY> environment variables (config keys only)
"""

from __future__ import annotations

import json
import os
from datetime import date, timedelta
from pathlib import Path

import yaml
from dotenv import load_dotenv

PROJECT_FILE = "pdt.yml"
APP_FILE = "config.yml"

PROVIDERS = ("google-cloud", "azure", "aws", "snowflake", "windows")
AWS_RUNTIMES = ("lambda", "fargate")
AZURE_RUNTIMES = ("functions", "container_apps")
SCHEDULE_SHORTHAND = {
    "hourly": "0 * * * *",
    "daily": "0 0 * * *",
    "weekly": "0 0 * * 0",
    "monthly": "0 0 1 * *",
    "yearly": "0 0 1 1 *",
}
APP_KEYS = {"name", "schedule", "timezone", "platform", "config", "env"}
PLATFORM_KEYS = {
    "provider", "region", "project",
    "account", "runtime",
    "subscription", "resource_group",
}
ENV_KEYS = {"required", "one_of", "optional"}
CRON_FIELDS = (
    ("minute", 0, 59),
    ("hour", 0, 23),
    ("day-of-month", 1, 31),
    ("month", 1, 12),
    ("day-of-week", 0, 7),
)


class ConfigError(Exception):
    pass


def load_yaml(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        data = yaml.safe_load(path.read_text())
    except yaml.YAMLError as e:
        raise ConfigError(f"{path}: not valid yaml: {e}")
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: expected a mapping at the top level")
    return data


def find_project(start: Path | None = None) -> Path:
    override = os.environ.get("PDT_PROJECT", "").strip()
    if override != "":
        folder = Path(override).expanduser().resolve()
        if not (folder / PROJECT_FILE).is_file():
            raise ConfigError(f"PDT_PROJECT is {folder}, which has no {PROJECT_FILE}")
        return folder
    folder = (start or Path.cwd()).resolve()
    while True:
        if (folder / PROJECT_FILE).is_file():
            return folder
        if folder.parent == folder:
            raise ConfigError(
                f"this is not a pdt project: no {PROJECT_FILE} here or in any "
                "parent folder. Run `pdt init` to set one up.")
        folder = folder.parent


def find_apps() -> list[str]:
    names = []
    for child in sorted(find_project().iterdir()):
        if child.name.startswith(".") or not child.is_dir():
            continue
        if (child / "run.py").is_file():
            names.append(child.name)
    return names


def uses_email(app: dict) -> bool:
    return "pdt.utils.send_email" in (app["dir"] / "run.py").read_text()


def root_app_entry(root_cfg: dict, name: str) -> dict:
    for entry in root_cfg.get("apps") or []:
        if isinstance(entry, dict) and entry.get("name") == name:
            return entry
    return {}


def env_overrides(name: str) -> dict:
    prefix = "PDT_" + name.upper().replace("-", "_") + "_"
    out = {}
    for key, val in os.environ.items():
        if key.startswith(prefix):
            out[key[len(prefix):].lower()] = val
    return out


def merged_app(name: str) -> dict:
    app_dir = find_project() / name
    if not (app_dir / "run.py").is_file():
        raise ConfigError(f"no app named {name!r} (no {name}/run.py)")
    root_cfg = load_yaml(find_project() / PROJECT_FILE)
    entry = root_app_entry(root_cfg, name)
    own = load_yaml(app_dir / APP_FILE)
    return {
        "name": name,
        "dir": app_dir,
        "schedule": own.get("schedule", entry.get("schedule")),
        "timezone": own.get("timezone", entry.get("timezone", "Etc/UTC")),
        "platform": {
            **(root_cfg.get("platform") or {}),
            **(entry.get("platform") or {}),
            **(own.get("platform") or {}),
        },
        "config": {
            **(entry.get("config") or {}),
            **(own.get("config") or {}),
            **env_overrides(name),
        },
        "env": own.get("env", entry.get("env")) or {},
    }


def save_platform_key(app: dict, key: str, value: str) -> Path:
    # Edit the text rather than rewrite the yaml, so the user's comments survive.
    project_file = find_project() / PROJECT_FILE
    root_cfg = load_yaml(project_file)
    if (root_cfg.get("platform") or {}).get("provider") == app["platform"].get("provider"):
        path = project_file
    else:
        path = app["dir"] / APP_FILE
    lines = path.read_text().splitlines() if path.is_file() else []
    start = next((i for i, line in enumerate(lines) if line.strip() == "platform:"), None)
    if start is None:
        lines += ["platform:", f'  {key}: "{value}"']
    else:
        end = start + 1
        while end < len(lines) and (lines[end].startswith((" ", "\t")) or lines[end].strip() == ""):
            end += 1
        block = range(start + 1, end)
        existing = next((i for i in block if lines[i].strip().startswith(f"{key}:")), None)
        if existing is not None:
            lines[existing] = f'{_indent(lines[existing])}{key}: "{value}"'
        else:
            first = next((i for i in block
                          if lines[i].strip() and not lines[i].strip().startswith("#")), None)
            indent = _indent(lines[first]) if first is not None else "  "
            lines.insert((first if first is not None else start) + 1, f'{indent}{key}: "{value}"')
    path.write_text("\n".join(lines) + "\n")
    return path


def _indent(line: str) -> str:
    return line[:len(line) - len(line.lstrip())]


def aws_account_problem(account: str) -> str:
    # Deploy reads the account from the credentials and writes it back, so
    # validate only judges a value the user already set.
    account = account.strip()
    if account == "" or (len(account) == 12 and account.isdigit()):
        return ""
    return "That is not an AWS account ID. It is 12 digits, for example 123456789012."


def cron_expression(schedule) -> str:
    if not isinstance(schedule, str) or schedule.strip() == "":
        raise ConfigError("schedule is missing")
    text = schedule.strip()
    if text.lstrip("@") in SCHEDULE_SHORTHAND:
        return SCHEDULE_SHORTHAND[text.lstrip("@")]
    fields = text.split()
    if len(fields) != 5:
        raise ConfigError(
            f"schedule {schedule!r} is not a shorthand "
            f"({', '.join(SCHEDULE_SHORTHAND)}) or a 5-field cron expression")
    for field, (label, lo, hi) in zip(fields, CRON_FIELDS):
        _cron_values(field, lo, hi, label)
    return text


def _cron_values(field: str, lo: int, hi: int, label: str) -> set[int]:
    values = set()
    for part in field.split(","):
        if part == "":
            raise ConfigError(f"cron {label} field {field!r} contains an empty value")
        span, separator, step_text = part.partition("/")
        if separator and (step_text == "" or "/" in step_text):
            raise ConfigError(f"cron {label} field {field!r} has an invalid step")
        try:
            step = int(step_text) if separator else 1
        except ValueError:
            raise ConfigError(f"cron {label} field {field!r} has a non-numeric step")
        if step < 1:
            raise ConfigError(f"cron {label} field {field!r} has a step below 1")
        if span == "*":
            start, end = lo, hi
        elif "-" in span:
            numbers = span.split("-")
            if len(numbers) != 2:
                raise ConfigError(f"cron {label} field {field!r} has an invalid range")
            try:
                start, end = (int(number) for number in numbers)
            except ValueError:
                raise ConfigError(f"cron {label} field {field!r} has a non-numeric range")
        else:
            try:
                start = int(span)
            except ValueError:
                raise ConfigError(f"cron {label} field {field!r} is not numeric")
            end = hi if separator else start
        if start < lo or end > hi or start > end:
            raise ConfigError(
                f"cron {label} field {field!r} must be between {lo} and {hi}")
        values.update(range(start, end + 1, step))
    return values


def runs_per_month(cron: str) -> float:
    cron = cron_expression(cron)
    minute, hour, dom, month, dow = cron.split()
    per_day = (len(_cron_values(minute, 0, 59, "minute"))
               * len(_cron_values(hour, 0, 23, "hour")))
    months = _cron_values(month, 1, 12, "month")
    doms = _cron_values(dom, 1, 31, "day-of-month")
    dows = {d % 7 for d in _cron_values(dow, 0, 7, "day-of-week")}
    days = 0
    for offset in range(365):  # a representative non-leap year
        day = date(2025, 1, 1) + timedelta(days=offset)
        if day.month not in months:
            continue
        dom_hit = day.day in doms
        dow_hit = (day.weekday() + 1) % 7 in dows
        # cron rule: dom and dow are OR'd only when both are restricted
        if dom == "*" or dow == "*":
            hit = dom_hit and dow_hit
        else:
            hit = dom_hit or dow_hit
        if hit:
            days += 1
    return per_day * days / 12


def find_env_files(start: Path) -> list[Path]:
    folder = start.resolve()
    files = []
    while True:
        candidate = folder / ".env"
        if candidate.is_file():
            files.append(candidate)
        at_root = (folder / PROJECT_FILE).is_file() or (folder / ".git").exists()
        if at_root or folder.parent == folder:
            break
        folder = folder.parent
    return files


def load_env(start: Path) -> list[Path]:
    # Closest .env wins; parents only fill keys still empty.
    # Existing process env wins (override=False).
    files = find_env_files(start)
    for path in files:
        load_dotenv(path, override=False)
    load_env_json()
    return files


def load_env_json() -> None:
    # In the cloud, deploy mounts all app secrets as one json blob.
    raw = os.environ.get("PDT_ENV_JSON", "").strip()
    if raw == "":
        return
    try:
        blob = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ConfigError(f"PDT_ENV_JSON is not valid json: {e}")
    for key, val in blob.items():
        os.environ.setdefault(key, str(val))


def check_env(env_spec: dict) -> list[str]:
    problems = []
    for name in env_spec.get("required") or []:
        if os.environ.get(name, "").strip() == "":
            problems.append(f"missing required env var {name}")
    groups = env_spec.get("one_of") or []
    if groups:
        satisfied = False
        for group in groups:
            complete = True
            for name in group:
                if os.environ.get(name, "").strip() == "":
                    complete = False
            if complete:
                satisfied = True
        if not satisfied:
            choices = " or ".join(" + ".join(group) for group in groups)
            problems.append(f"set one of: {choices}")
    return problems


def validate() -> list[str]:
    problems = []
    try:
        root_cfg = load_yaml(find_project() / PROJECT_FILE)
    except ConfigError as e:
        return [str(e)]
    for key in root_cfg:
        if key not in {"platform", "apps"}:
            problems.append(f"{PROJECT_FILE}: unknown key {key!r}")
    apps = find_apps()
    for entry in root_cfg.get("apps") or []:
        if not isinstance(entry, dict) or "name" not in entry:
            problems.append(f"{PROJECT_FILE}: every apps entry needs a name")
            continue
        if entry["name"] not in apps:
            problems.append(f"{PROJECT_FILE}: app {entry['name']!r} has no directory with a run.py")
        for key in entry:
            if key not in APP_KEYS:
                problems.append(f"{PROJECT_FILE}: app {entry['name']}: unknown key {key!r}")
    for name in apps:
        problems.extend(validate_app(name))
    return problems


def validate_app(name: str) -> list[str]:
    problems = []
    where = f"{name}/config.yml"
    try:
        own = load_yaml(find_project() / name / APP_FILE)
    except ConfigError as e:
        return [str(e)]
    for key in own:
        if key == "apps":
            problems.append(f"{where}: the apps list is only allowed in pdt.yml")
        elif key not in APP_KEYS:
            problems.append(f"{where}: unknown key {key!r}")
    own_name = own.get("name")
    if own_name is not None and own_name != name:
        problems.append(f"{where}: name {own_name!r} does not match directory {name!r}")
    try:
        app = merged_app(name)
    except ConfigError as e:
        return problems + [str(e)]
    for key in app["platform"]:
        if key not in PLATFORM_KEYS:
            problems.append(f"{name}: platform: unknown key {key!r}")
    provider = app["platform"].get("provider")
    if provider not in PROVIDERS:
        problems.append(
            f"{name}: platform.provider must be one of: {', '.join(PROVIDERS)}. "
            f"Set it under platform: in {PROJECT_FILE}, or in {name}/{APP_FILE}.")
    if provider == "aws":
        problem = aws_account_problem(str(app["platform"].get("account") or ""))
        if problem != "":
            problems.append(f"{name}: platform.account: {problem}")
        runtime = app["platform"].get("runtime", "lambda")
        if runtime not in AWS_RUNTIMES:
            problems.append(f"{name}: platform.runtime must be one of: {', '.join(AWS_RUNTIMES)}")
    if provider == "azure":
        runtime = app["platform"].get("runtime", "functions")
        if runtime not in AZURE_RUNTIMES:
            problems.append(f"{name}: platform.runtime must be one of: {', '.join(AZURE_RUNTIMES)}")
    if app["schedule"] is not None:
        try:
            cron_expression(app["schedule"])
        except ConfigError as e:
            problems.append(f"{name}: {e}")
    for key in app["env"]:
        if key not in ENV_KEYS:
            problems.append(f"{name}: env: unknown key {key!r}")
    return problems
