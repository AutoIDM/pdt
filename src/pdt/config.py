"""Load, merge, and validate pdt configuration.

A project is a directory holding pdt.yml. An app is a directory inside it
that contains run.py, or one that holds PowerShell scripts (.ps1) and no
run.py; `pdt.powershell` describes how those run. Commands find the
project by walking up from the working directory, so pdt works the same
whether it was installed from PyPI or run from a clone of this repository.

Merge order for one app, least to most specific:
  1. `platform:` defaults in the project pdt.yml
  2. the app's entry under `apps:` in pdt.yml
  3. the app's own config.yml
  4. PDT_<APP>_<KEY> environment variables (config keys only)
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from contextlib import contextmanager
from datetime import date, timedelta
from pathlib import Path

import yaml
from dotenv import dotenv_values, load_dotenv

from pdt import console

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
APP_KEYS = {"name", "schedule", "timezone", "platform", "config", "env", "storage", "enabled",
            "run_scripts", "continue_on_error", "pause"}
PLATFORM_KEYS = {
    "provider", "region", "project",
    "account", "profile",
    "subscription", "resource_group", "environment",
    "timezone",
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
    "config": APP_LEVEL,
    "env": APP_LEVEL,
    "storage": APP_LEVEL,
    "enabled": APP_LEVEL,
    "run_scripts": APP_LEVEL,
    "continue_on_error": APP_LEVEL,
    "pause": APP_LEVEL,
    **{key: "the platform: section" for key in PLATFORM_KEYS},
    "timezone": f"the platform: section, or {APP_LEVEL}",
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


def project_line() -> str:
    """Which project folder pdt works on, and how it found it."""
    project = console.value(str(find_project()))
    if os.environ.get("PDT_PROJECT", "").strip() != "":
        return f"Project folder: {project}  [dim](from {console.value('PDT_PROJECT')})[/]"
    return (f"Project folder: {project}  "
            f"[dim](the first folder with {console.value(PROJECT_FILE)}, from here up)[/]")


def powershell_scripts(app_dir: Path) -> list[str]:
    """The .ps1 files of a PowerShell app, in name order; [] for a Python app. A folder with a
    run.py is a Python app, unless it also holds the requirements.psd1 that
    `pdt new APP --from-scripts` writes next to it."""
    if not app_dir.is_dir():
        return []
    if (app_dir / "run.py").is_file() and not (app_dir / "requirements.psd1").is_file():
        return []
    return sorted(path.name for path in app_dir.iterdir()
                  if path.is_file() and path.suffix.lower() == ".ps1")


def is_app(folder: Path) -> bool:
    return (folder / "run.py").is_file() or powershell_scripts(folder) != []


def app_folders() -> list[str]:
    names = []
    for child in sorted(find_project().iterdir()):
        if child.name.startswith(".") or not child.is_dir():
            continue
        if is_app(child):
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


def current_app() -> str | None:
    """The enabled app whose folder holds the working folder, or None."""
    try:
        parts = Path.cwd().resolve().relative_to(find_project()).parts
    except ValueError:
        return None
    return parts[0] if parts and parts[0] in find_apps() else None


def uses_email(app: dict) -> bool:
    run_py = app["dir"] / "run.py"
    return run_py.is_file() and "pdt.utils.send_email" in run_py.read_text()


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


def app_name_problem(name: str) -> str:
    if name in ("", ".", "..") or any(c in name for c in "/\\\0") or Path(name).name != name:
        return f"{name!r} is not an app name; use one folder name with no path separators"
    return ""


def merged_app(name: str) -> dict:
    problem = app_name_problem(name)
    if problem != "":
        raise ConfigError(problem)
    app_dir = find_project() / name
    if not is_app(app_dir):
        raise ConfigError(f"no app named {name!r} (no {name}/run.py and no .ps1 file in {name}/)")
    root_cfg = load_yaml(find_project() / PROJECT_FILE)
    entry = root_app_entry(root_cfg, name)
    own = load_yaml(app_dir / APP_FILE)
    entry_where = f"{PROJECT_FILE}: apps entry {name!r}"
    own_where = f"{name}/{APP_FILE}"
    platform = {
        **mapping(root_cfg, "platform", PROJECT_FILE),
        **mapping(entry, "platform", entry_where),
        **mapping(own, "platform", own_where),
    }
    default_timezone = platform.get(
        "timezone", "local" if platform.get("provider") == "windows" else "Etc/UTC")
    return {
        "name": name,
        "dir": app_dir,
        "schedule": own.get("schedule", entry.get("schedule")),
        "timezone": own.get("timezone", entry.get("timezone", default_timezone)),
        "platform": platform,
        "config": {
            **mapping(entry, "config", entry_where),
            **mapping(own, "config", own_where),
            **env_overrides(name),
        },
        "env": mapping(own, "env", own_where) or mapping(entry, "env", entry_where),
        "storage": own.get("storage", entry.get("storage", True)),
        "enabled": own.get("enabled", entry.get("enabled", True)),
        "run_scripts": own.get("run_scripts", entry.get("run_scripts")),
        "continue_on_error": own.get("continue_on_error", entry.get("continue_on_error", False)),
        "pause": own.get("pause", entry.get("pause", False)),
    }


def save_app_key(app: dict, key: str, value) -> Path:
    """Write one top-level key into the app's config.yml, the most specific file.

    Edits the text rather than rewriting the yaml, so the user's comments survive,
    and holds the same lock and atomic replace as `save_platform_key`.
    """
    path = app["dir"] / APP_FILE
    text = ("true" if value else "false") if isinstance(value, bool) else yaml_quoted(value)
    with locked(path):
        lines = path.read_text().splitlines() if path.is_file() else []
        existing = next((i for i, line in enumerate(lines)
                         if not line.startswith((" ", "\t")) and line.split("#")[0].strip()
                         .startswith(f"{key}:")), None)
        if existing is not None:
            lines[existing] = f"{key}: {text}"
        else:
            lines.append(f"{key}: {text}")
        write_text_atomically(path, "\n".join(lines) + "\n")
    return path


def save_platform_key(app: dict, key: str, value: str) -> Path:
    # Edit the text rather than rewrite the yaml, so the user's comments survive.
    project_file = find_project() / PROJECT_FILE
    root_cfg = load_yaml(project_file)
    if (root_cfg.get("platform") or {}).get("provider") == app["platform"].get("provider"):
        path = project_file
    else:
        path = app["dir"] / APP_FILE
    with locked(path):
        lines = path.read_text().splitlines() if path.is_file() else []
        start = next((i for i, line in enumerate(lines) if line.strip() == "platform:"), None)
        if start is None:
            lines += ["platform:", f"  {key}: {yaml_quoted(value)}"]
        else:
            end = start + 1
            while end < len(lines) and (lines[end].startswith((" ", "\t")) or lines[end].strip() == ""):
                end += 1
            block = range(start + 1, end)
            existing = next((i for i in block if lines[i].strip().startswith(f"{key}:")), None)
            if existing is not None:
                lines[existing] = f"{_indent(lines[existing])}{key}: {yaml_quoted(value)}"
            else:
                first = next((i for i in block
                              if lines[i].strip() and not lines[i].strip().startswith("#")), None)
                indent = _indent(lines[first]) if first is not None else "  "
                lines.insert((first if first is not None else start) + 1, f"{indent}{key}: {yaml_quoted(value)}")
        write_text_atomically(path, "\n".join(lines) + "\n")
    return path


def set_app_enabled(name: str, enabled: bool) -> Path:
    return save_app_key({"dir": find_project() / name}, "enabled", enabled)


def _indent(line: str) -> str:
    return line[:len(line) - len(line.lstrip())]


def yaml_quoted(value: str) -> str:
    escaped = str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    return f'"{escaped}"'


def write_text_atomically(path: Path, text: str) -> None:
    # The temporary file sits in the same folder because a rename is atomic only
    # within one filesystem.
    tmp = tempfile.NamedTemporaryFile("w", dir=path.parent, prefix=path.name + ".",
                                      suffix=".tmp", delete=False)
    try:
        with tmp:
            tmp.write(text)
            tmp.flush()
            os.fsync(tmp.fileno())
        os.replace(tmp.name, path)
    except BaseException:
        os.unlink(tmp.name)
        raise


@contextmanager
def locked(path: Path):
    # Hold an exclusive lock on a sibling .lock file while the caller reads and rewrites path.
    with open(path.with_name(path.name + ".lock"), "w") as handle:
        fd = handle.fileno()
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)


STATE_DIR = ".pdt"


def _state_path() -> Path:
    return find_project() / STATE_DIR / "state"


def read_state() -> dict:
    path = _state_path()
    if not path.is_file():
        return {}
    try:
        state = json.loads(path.read_text() or "{}")
    except ValueError:
        state = None
    if not isinstance(state, dict):
        console.warn(f"{STATE_DIR}/{path.name} is not valid; treating every app as not deployed")
        return {}
    return state


def write_state(state: dict) -> None:
    path = _state_path()
    path.parent.mkdir(exist_ok=True)
    # Self-ignoring, so projects created before this dir existed stay clean.
    (path.parent / ".gitignore").write_text("*\n")
    write_text_atomically(path, json.dumps(state, indent=2) + "\n")


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


DAY_WORDS = ("Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday")
MONTH_WORDS = ("January", "February", "March", "April", "May", "June", "July", "August",
               "September", "October", "November", "December")


def _words(items) -> str:
    items = list(items)
    if len(items) == 1:
        return items[0]
    return ", ".join(items[:-1]) + " and " + items[-1]


def _ordinal(day: int) -> str:
    suffix = "th" if 11 <= day % 100 <= 13 else {1: "st", 2: "nd", 3: "rd"}.get(day % 10, "th")
    return f"{day}{suffix}"


def _minutes_past(minutes: list[int]) -> str:
    if minutes == [0]:
        return "on the hour"
    return _words(f"{m} minute{'s' if m != 1 else ''}" if m else "0 minutes"
                  for m in minutes) + " past the hour"


def _day_words(days: list[int]) -> str:
    """Monday to Friday for a run of days, else each day named."""
    if len(days) >= 3 and days == list(range(days[0], days[-1] + 1)):
        return f"{DAY_WORDS[days[0]]} to {DAY_WORDS[days[-1]]}"
    return _words(DAY_WORDS[d] for d in days)


def _clock_times(minute: str, hour: str) -> list[str] | None:
    """The HH:MM times a minute and hour field name, or None when they are not plain lists."""
    if any(mark in field for field in (minute, hour) for mark in ("*", "/")):
        return None
    minutes = sorted(_cron_values(minute, 0, 59, "minute"))
    hours = sorted(_cron_values(hour, 0, 23, "hour"))
    if len(minutes) * len(hours) > 12:
        return None
    return [f"{h:02d}:{m:02d}" for h in hours for m in minutes]


def describe_schedule(schedule) -> str:
    """A schedule in the words an administrator would use, for a tooltip or a listing.

    Covers the shapes a job has: every N minutes, hourly, daily, on weekdays,
    on days of the month. Anything else stays as the cron expression.
    """
    cron = cron_expression(schedule)
    minute, hour, dom, month, dow = cron.split()
    if hour == dom == month == dow == "*":
        if minute == "*":
            return "every minute"
        if minute.startswith("*/"):
            return f"every {minute[2:]} minutes"
        if "/" not in minute:
            minutes = sorted(_cron_values(minute, 0, 59, "minute"))
            return f"{_minutes_past(minutes)}, every hour"
    if hour.startswith("*/") and dom == month == dow == "*" and minute.isdigit():
        return f"every {hour[2:]} hours, {_minutes_past([int(minute)])}"
    times = _clock_times(minute, hour)
    if times is None:
        return f"cron {cron}"
    when = "at " + _words(times)
    if dom == month == dow == "*":
        return f"every day {when}"
    if dom == month == "*":
        days = sorted({d % 7 for d in _cron_values(dow, 0, 7, "day-of-week")})
        return f"every {_day_words(days)} {when}"
    if dow == "*":
        days = _words(_ordinal(d) for d in sorted(_cron_values(dom, 1, 31, "day-of-month")))
        if month == "*":
            return f"on the {days} of every month {when}"
        months = _words(MONTH_WORDS[m - 1] for m in sorted(_cron_values(month, 1, 12, "month")))
        return f"on the {days} of {months} {when}"
    return f"cron {cron}"


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


def running_app_dir() -> Path:
    """The app folder of this process: where the running run.py is, else the working folder."""
    main = getattr(sys.modules.get("__main__"), "__file__", None)
    if main is not None and Path(main).name == "run.py":
        return Path(main).resolve().parent
    return Path.cwd()


def shown_path(path: Path) -> str:
    """`path` from the project folder when it is inside it, else in full."""
    try:
        return str(path.relative_to(find_project()))
    except (ValueError, ConfigError):
        return str(path)


def env_file_lines(start: Path) -> list[str]:
    """What load_env(start) reads, for the user: the .env files, and each name this
    terminal already sets with another value, which the .env value does not replace.
    Call it before load_env, which adds the .env values to the environment."""
    files = find_env_files(start)
    env = console.value(".env")
    if not files:
        return [f"no {env} file in {console.value(start.name)} or a folder above it, "
                "so pdt reads env vars only from this terminal"]
    if len(files) == 1:
        lines = [f"{env} file read: {console.value(shown_path(files[0]))}"]
    else:
        lines = [f"{env} files read, the first one wins: "
                 f"{', '.join(console.value(shown_path(path)) for path in files)}"]
    kept = sorted({name for path in files for name, value in dotenv_values(path).items()
                   if name in os.environ and os.environ[name] != (value or "")})
    if kept:
        lines.append(f"this terminal already sets {', '.join(map(console.value, kept))}, "
                     f"so pdt uses that value and not the one in {env}")
    return lines


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


def env_spec(app: dict) -> dict:
    """The app's env spec, with each env var the app's code reads that config.yml does not
    list: required, with `read_by` naming the file and line of its first read, or optional
    for a Python read that has a fallback (`os.environ.get`, `os.getenv`)."""
    spec = app["env"]
    if not powershell_scripts(app["dir"]):
        from pdt import python_env
        read_by, optional = python_env.unlisted_env_reads(app["dir"], spec)
        if not read_by and not optional:
            return spec
        return {**spec, "required": [*(spec.get("required") or []), *read_by],
                "optional": [*(spec.get("optional") or []), *optional], "read_by": read_by}
    from pdt import powershell, pwsh
    try:
        read_by = powershell.unlisted_env_reads(powershell.extract(app["dir"]), spec)
    except (pwsh.PwshError, powershell.PowerShellError) as e:
        raise ConfigError(str(e))
    return {**spec, "required": [*(spec.get("required") or []), *read_by], "read_by": read_by}


def found_env_lines(app: dict) -> list[str]:
    """The env vars the app's code reads that config.yml does not list, as lines for
    the user, or none. Raises ConfigError, like env_spec."""
    spec = env_spec(app)
    found = [(name, f"required, {_code_line(where)}")
             for name, where in (spec.get("read_by") or {}).items()]
    listed = set(app["env"].get("optional") or [])
    optional = [name for name in spec.get("optional") or [] if name not in listed]
    if optional:
        from pdt import python_env
        first: dict[str, str] = {}
        for where, name, _kind in python_env.reads(app["dir"]):
            first.setdefault(name, _code_line(where))
        found += [(name, f"optional, {first.get(name, 'the code')}") for name in optional]
    if not found:
        return []
    return console.rows(f"env vars the code reads that {console.value(APP_FILE)} does not list:",
                        found)


def _code_line(where: str) -> str:
    """`report.ps1:4` as `report.ps1 line 4`, the file name bold."""
    file, _, line = where.rpartition(":")
    return f"{console.value(file)} line {line}"


def check_env(env_spec: dict, values=None) -> list[str]:
    if values is None:
        values = os.environ
    problems = []
    read_by = env_spec.get("read_by") or {}
    for name in env_spec.get("required") or []:
        if is_set(values, name):
            continue
        if name in read_by:
            read = f'os.environ["{name}"]' if read_by[name].split(":")[0].endswith(".py") else f"$env:{name}"
            problems.append(f"missing required env var {name} ({read_by[name]} reads {read})")
        else:
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


def missing_env(app: dict) -> str:
    """check_env's problems for `app` as one message, or "" when nothing is missing.

    The message names each missing var, where pdt looked, and the fix. A
    deployed job (PDT_ENV_SECRET_RESOURCE set) reads the app's cloud secret,
    so its fix is `pdt secrets <app> save`; anywhere else the fix is a .env line.
    A var that only a script's read requires can also move to env: optional:.
    """
    try:
        spec = env_spec(app)
    except ConfigError as e:
        return str(e)
    problems = check_env(spec)
    if not problems:
        return ""
    secret = os.environ.get("PDT_ENV_SECRET_RESOURCE", "").strip()
    files = find_env_files(app["dir"])
    if secret != "":
        looked = f"the app's cloud secret {secret}"
        fix = (f"add each one as NAME=value to the .env file in your pdt project, "
               f"then run `pdt secrets {app['name']} save`")
    else:
        if files:
            looked = f"{', '.join(str(path) for path in files)} and the environment"
            target = files[0]
        else:
            try:
                target = find_project(app["dir"]) / ".env"
            except ConfigError:
                target = app["dir"] / ".env"
            looked = (f"the environment; there is no .env file in {app['dir']} "
                      "or a folder above it")
        fix = f"add each one as NAME=value to {target}, then run the command again"
    message = f"{'; '.join(problems)}. pdt looked in {looked}. To fix it, {fix}."
    if any(not is_set(os.environ, name) for name in spec.get("read_by") or {}):
        message += (f" If a script works without one that it reads, list that one under "
                    f"env: optional: in {app['name']}/{APP_FILE} instead.")
    return message


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
            problems.append(f"{PROJECT_FILE}: app {entry['name']!r} has no directory with a run.py or a .ps1 file")
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
    if not isinstance(app["continue_on_error"], bool):
        problems.append(f"{where}: continue_on_error must be true or false")
    problems.extend(f"{where}: {problem}" for problem in run_scripts_problems(app))
    if not isinstance(app["pause"], bool):
        problems.append(f"{where}: pause must be true or false")
    return problems


def run_scripts_problems(app: dict) -> list[str]:
    scripts = app["run_scripts"]
    if scripts is None:
        return []
    available = powershell_scripts(app["dir"])
    if not available:
        return ["run_scripts only applies to an app made of .ps1 files; remove the key"]
    if any(script.lower() == "run.ps1" for script in available):
        return ["run.ps1 and run_scripts cannot both be present; keep one"]
    not_a_list = "run_scripts must be a list of .ps1 file names in the order to run them"
    if not isinstance(scripts, list) or scripts == []:
        return [not_a_list]
    if not all(isinstance(script, str) for script in scripts):
        return [not_a_list]
    return [f"run_scripts names {script!r}, which is not a .ps1 file in {app['name']}/"
            for script in scripts if script not in available]
