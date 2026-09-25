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

PROVIDERS = ("google-cloud", "azure", "aws", "windows")
# Every cloud provider runs a job as a container image built from the app
# folder: AWS on Fargate, Azure on Container Apps Jobs, Google Cloud on
# Cloud Run Jobs. The windows provider runs the app directly.
CONTAINER_PROVIDERS = ("google-cloud", "aws", "azure")
SCHEDULE_SHORTHAND = {
    "hourly": "0 * * * *",
    "daily": "0 0 * * *",
    "weekly": "0 0 * * 0",
    "monthly": "0 0 1 * *",
    "yearly": "0 0 1 1 *",
}
ROOT_KEYS = {"platform", "apps"}
APP_KEYS = {"name", "schedule", "timezone", "platform", "config", "env", "storage", "enabled"}
PLATFORM_KEYS = {
    "provider", "region", "project",
    "account", "profile",
    "subscription", "resource_group", "environment",
}
ENV_KEYS = {"required", "one_of", "optional"}
# Where each known key belongs, so a key in the wrong section gets told
# where to move instead of "unknown key".
APP_LEVEL = f"the top level of the app's {APP_FILE}, or its apps: entry in {PROJECT_FILE}"
KEY_HOME = {
    "apps": f"the top level of {PROJECT_FILE}",
    "platform": f"the top level of {PROJECT_FILE} or of the app's {APP_FILE}",
    "name": f"the app's apps: entry in {PROJECT_FILE}",
    "schedule": APP_LEVEL,
    "timezone": APP_LEVEL,
    "config": APP_LEVEL,
    "env": APP_LEVEL,
    "storage": APP_LEVEL,
    "enabled": APP_LEVEL,
    **{key: "the platform: section" for key in PLATFORM_KEYS},
    **{key: "the env: section" for key in ENV_KEYS},
}
# Keys that belong nowhere any more. The message says what to do instead.
RETIRED_KEYS = {
    "runtime": "is no longer a setting: AWS always runs jobs on Fargate and Azure on "
               "Container Apps Jobs; remove the key",
}
# config: is free-form, so only keys that shape the app are rejected there.
CONFIG_REJECTS = (APP_KEYS | {"apps"}) - {"name"}
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


def data_home() -> Path:
    """The user's data folder, where pdt keeps what it writes outside a project."""
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData/Local")
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share")


def machine_data_home() -> Path:
    """The machine-wide data folder on Windows, %ProgramData%\\pdt, where a scheduled
    task that runs as SYSTEM and the user who deployed it both reach an app's files."""
    return Path(os.environ.get("ProgramData") or r"C:\ProgramData") / "pdt"


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


def app_folders() -> list[str]:
    names = []
    for child in sorted(find_project().iterdir()):
        if child.name.startswith(".") or not child.is_dir():
            continue
        if (child / "run.py").is_file():
            names.append(child.name)
    return names


def is_enabled(name: str) -> bool:
    project = find_project()
    try:
        entry = root_app_entry(load_yaml(project / PROJECT_FILE), name)
        own = load_yaml(project / name / APP_FILE)
    except ConfigError:
        return True
    return own.get("enabled", entry.get("enabled", True)) is not False


def find_apps() -> list[str]:
    """The apps every command acts on. `enabled: false` takes an app out."""
    return [name for name in app_folders() if is_enabled(name)]


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


def mapping(cfg: dict, key: str, where: str) -> dict:
    value = cfg.get(key)
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ConfigError(f"{where}: {key}: must be a mapping of key: value lines")
    return value


def merged_app(name: str) -> dict:
    app_dir = find_project() / name
    if not (app_dir / "run.py").is_file():
        raise ConfigError(f"no app named {name!r} (no {name}/run.py)")
    root_cfg = load_yaml(find_project() / PROJECT_FILE)
    entry = root_app_entry(root_cfg, name)
    own = load_yaml(app_dir / APP_FILE)
    entry_where = f"{PROJECT_FILE}: apps entry {name!r}"
    own_where = f"{name}/{APP_FILE}"
    return {
        "name": name,
        "dir": app_dir,
        "schedule": own.get("schedule", entry.get("schedule")),
        "timezone": own.get("timezone", entry.get("timezone", "Etc/UTC")),
        "platform": {
            **mapping(root_cfg, "platform", PROJECT_FILE),
            **mapping(entry, "platform", entry_where),
            **mapping(own, "platform", own_where),
        },
        "config": {
            **mapping(entry, "config", entry_where),
            **mapping(own, "config", own_where),
            **env_overrides(name),
        },
        "env": mapping(own, "env", own_where) or mapping(entry, "env", entry_where),
        "storage": own.get("storage", entry.get("storage", True)),
        "enabled": own.get("enabled", entry.get("enabled", True)),
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


STATE_DIR = ".pdt"


def _state_path() -> Path:
    return find_project() / STATE_DIR / "state"


def read_state() -> dict:
    path = _state_path()
    if not path.is_file():
        return {}
    return json.loads(path.read_text() or "{}")


def write_state(state: dict) -> None:
    path = _state_path()
    path.parent.mkdir(exist_ok=True)
    # Self-ignoring, so projects created before this dir existed stay clean.
    (path.parent / ".gitignore").write_text("*\n")
    path.write_text(json.dumps(state, indent=2) + "\n")


def mark_deployed(name: str, deployed: bool) -> None:
    state = read_state()
    apps = set(state.get("deployed") or [])
    if deployed:
        apps.add(name)
    else:
        apps.discard(name)
    state["deployed"] = sorted(apps)
    write_state(state)


def is_deployed(name: str) -> bool:
    return name in (read_state().get("deployed") or [])


def aws_account_problem(account: str) -> str:
    # Deploy reads the account from the credentials and writes it back, so
    # validate only judges a value the user already set.
    account = account.strip()
    if account == "" or (len(account) == 12 and account.isdigit()):
        return ""
    return "That is not an AWS account ID. It is 12 digits, for example 123456789012."


def azure_environment_problem(environment: str) -> str:
    environment = environment.strip()
    if environment == "":
        return ""
    group, _, name = environment.partition("/")
    if group and name and "/" not in name and " " not in environment:
        return ""
    return ("That is not a Container Apps environment. Write it as "
            "<resource-group>/<name>, for example my-group/my-environment.")


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
    # In the cloud, deploy mounts all app secrets as one json blob. Azure
    # serves the copy it cached at deploy time, so read the secret itself.
    raw = os.environ.get("PDT_ENV_JSON", "").strip()
    if os.environ.get("PDT_ENV_SECRET_RESOURCE", "").strip() != "":
        from pdt.utils import env_secret
        raw = env_secret.current() or raw
    if raw == "":
        return
    try:
        blob = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ConfigError(f"PDT_ENV_JSON is not valid json: {e}")
    for key, val in blob.items():
        os.environ.setdefault(key, str(val))


def is_set(values, name: str) -> bool:
    """<NAME>_B64 satisfies <NAME>_PATH, since deploy turns one into the other."""
    if str(values.get(name, "")).strip() != "":
        return True
    return name.endswith("_PATH") and str(values.get(name[:-5] + "_B64", "")).strip() != ""


def check_env(env_spec: dict, values=None) -> list[str]:
    if values is None:
        values = os.environ
    problems = []
    for name in env_spec.get("required") or []:
        if not is_set(values, name):
            problems.append(f"missing required env var {name}")
    groups = env_spec.get("one_of") or []
    if groups:
        satisfied = False
        for group in groups:
            complete = True
            for name in group:
                if not is_set(values, name):
                    complete = False
            if complete:
                satisfied = True
        if not satisfied:
            choices = " or ".join(" + ".join(group) for group in groups)
            problems.append(f"set one of: {choices}")
    return problems


def key_problems(where: str, section: dict, allowed: set[str] | None) -> list[str]:
    # allowed=None means free-form (the config: section).
    problems = []
    for key in section:
        if allowed is None and key not in CONFIG_REJECTS:
            continue
        if allowed is not None and key in allowed:
            continue
        if key in RETIRED_KEYS:
            problems.append(f"{where}: {key!r} {RETIRED_KEYS[key]}")
        elif key in KEY_HOME:
            problems.append(f"{where}: {key!r} belongs in {KEY_HOME[key]}, not here")
        else:
            problems.append(f"{where}: unknown key {key!r}")
    return problems


def app_section_problems(where: str, section: dict) -> list[str]:
    problems = key_problems(where, section, APP_KEYS)
    for key, allowed in (("platform", PLATFORM_KEYS), ("config", None), ("env", ENV_KEYS)):
        try:
            block = mapping(section, key, where)
        except ConfigError as e:
            problems.append(str(e))
            continue
        problems.extend(key_problems(f"{where}: {key}", block, allowed))
        if key == "env":
            problems.extend(env_shape_problems(f"{where}: env", block))
    return problems


def env_shape_problems(where: str, env: dict) -> list[str]:
    problems = []
    for key in ("required", "optional"):
        names = env.get(key)
        if names is not None and not (
                isinstance(names, list) and all(isinstance(n, str) for n in names)):
            problems.append(f"{where}: {key} must be a list of env var names")
    groups = env.get("one_of")
    if groups is not None and not (
            isinstance(groups, list)
            and all(isinstance(g, list) and all(isinstance(n, str) for n in g) for g in groups)):
        problems.append(f"{where}: one_of must be a list of lists of env var names")
    return problems


def validate() -> list[str]:
    problems = []
    try:
        root_cfg = load_yaml(find_project() / PROJECT_FILE)
    except ConfigError as e:
        return [str(e)]
    problems.extend(key_problems(PROJECT_FILE, root_cfg, ROOT_KEYS))
    apps = find_apps()
    entries = root_cfg.get("apps")
    if entries is not None and not isinstance(entries, list):
        problems.append(f"{PROJECT_FILE}: apps must be a list, one `- name: <app>` per app")
        entries = []
    for entry in entries or []:
        if not isinstance(entry, dict) or "name" not in entry:
            problems.append(f"{PROJECT_FILE}: every apps entry needs a name")
            continue
        if entry["name"] not in app_folders():
            problems.append(f"{PROJECT_FILE}: app {entry['name']!r} has no directory with a run.py")
    for name in apps:
        problems.extend(validate_app(name))
    # Every app re-checks the shared platform: block; report it once.
    return sorted(set(problems), key=problems.index)


def builds_image(platform: dict) -> bool:
    """True when deploying with this platform builds a container image."""
    return platform.get("provider") in CONTAINER_PROVIDERS


def dockerfile_problem(platform: dict) -> str:
    """Why an app's own Dockerfile would be ignored, or "" when it is used."""
    if builds_image(platform):
        return ""
    return f"not used by the {platform.get('provider')} provider; remove the file"


def validate_app(name: str) -> list[str]:
    where = f"{name}/{APP_FILE}"
    try:
        root_cfg = load_yaml(find_project() / PROJECT_FILE)
        own = load_yaml(find_project() / name / APP_FILE)
    except ConfigError as e:
        return [str(e)]
    problems = []
    try:
        platform = mapping(root_cfg, "platform", PROJECT_FILE)
    except ConfigError as e:
        problems.append(str(e))
        platform = {}
    problems.extend(key_problems(f"{PROJECT_FILE}: platform", platform, PLATFORM_KEYS))
    entry = root_app_entry(root_cfg, name)
    problems.extend(app_section_problems(f"{PROJECT_FILE}: apps entry {name!r}", entry))
    problems.extend(app_section_problems(where, own))
    own_name = own.get("name")
    if own_name is not None and own_name != name:
        problems.append(f"{where}: name {own_name!r} does not match directory {name!r}")
    try:
        app = merged_app(name)
    except ConfigError as e:
        if str(e) not in problems:
            problems.append(str(e))
        return problems
    provider = app["platform"].get("provider")
    if provider not in PROVIDERS:
        problems.append(
            f"{name}: platform.provider must be one of: {', '.join(PROVIDERS)}. "
            f"Set it under platform: in {PROJECT_FILE}, or in {name}/{APP_FILE}.")
    if provider == "aws":
        problem = aws_account_problem(str(app["platform"].get("account") or ""))
        if problem != "":
            problems.append(f"{name}: platform.account: {problem}")
    if provider == "azure":
        problem = azure_environment_problem(str(app["platform"].get("environment") or ""))
        if problem != "":
            problems.append(f"{name}: platform.environment: {problem}")
    dockerfile = find_project() / name / "Dockerfile"
    if dockerfile.is_file():
        problem = dockerfile_problem(app["platform"])
        if problem != "":
            problems.append(f"{name}/Dockerfile: {problem}")
    if app["schedule"] is not None:
        try:
            cron_expression(app["schedule"])
        except ConfigError as e:
            problems.append(f"{name}: {e}")
    if not isinstance(app["storage"], bool):
        problems.append(f"{where}: storage must be true or false")
    if not isinstance(app["enabled"], bool):
        problems.append(f"{where}: enabled must be true or false")
    return problems
