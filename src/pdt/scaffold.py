"""Create a project (`pdt init`) and add apps to it (`pdt new`)."""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

from pdt import __version__, config, console, deploy, powershell, pwsh, regions, watch
from pdt.config import (APP_FILE, PROJECT_FILE, ConfigError, app_name_problem, find_project,
                        merged_app, powershell_scripts)
from pdt.utils.env_secret import private_file

EXAMPLES = Path(__file__).resolve().parent / "examples"
STARTER = "hello-world"

ART = """
                   +  ·.    .     .    .·  °
            ·.·  +  .*  .°.   +··   °°·  ·*  +  ·++
             · * .* ° ·  .  ++·..·°  ·  . · *. . .
          °.+··°   ██████╗ ██████╗ ████████╗   *··°··
       ·   ·.·*    ██╔══██╗██╔══██╗╚══██╔══╝    ··*°   °
                ·  ██████╔╝██║  ██║   ██║     ·
            ···    ██╔═══╝ ██║  ██║   ██║       °°·
       .    ++ *°  ██║     ██████╔╝   ██║     *. ..    ·
         ° +  °·   ╚═╝     ╚═════╝    ╚═╝      .*  + °
         · .   ·  °·                       °.  *   . +

                      PROCESS DEPLOY TOOL

                           welcome!
"""

PROVIDER_CHOICES = [
    ("azure", "Microsoft Azure"),
    ("aws", "Amazon Web Services"),
    ("google-cloud", "Google Cloud"),
    ("windows", "This Windows PC, using Task Scheduler"),
    ("", "Decide later"),
]
NAMES = {**regions.PROVIDER_NAMES, "windows": "Windows Task Scheduler"}
PLATFORM_TEMPLATE = "#platform:"


def _needed(answer: str) -> str:
    return "" if answer.strip() != "" else "This one is needed. Please type a value."


TIMEZONE_QUESTION = ("timezone", "Which time zone should the schedules use?",
                     regions.local_timezone, _needed)

# Ask only what pdt cannot supply. Every provider learns its own account,
# subscription, or project from the credentials at deploy time and writes the
# answer back.
PROVIDER_QUESTIONS = {
    "azure": [
        ("region", regions.question("azure"), lambda: regions.suggest_region("azure"), _needed),
    ],
    "aws": [
        ("region", regions.question("aws"), lambda: regions.suggest_region("aws"), _needed),
        TIMEZONE_QUESTION,
    ],
    "google-cloud": [
        ("region", regions.question("google-cloud"),
         lambda: regions.suggest_region("google-cloud"), _needed),
        TIMEZONE_QUESTION,
    ],
    "windows": [],
    "": [],
}

SYSTEM_FOLDERS = (
    "/usr", "/etc", "/bin", "/sbin", "/opt", "/var", "/Library", "/System",
    "/Applications", "/private", "/tmp", "C:\\Windows", "C:\\Program Files",
)

GITIGNORE_TEXT = """\
.env
.env.*
.secrets/
.pdt/
*.lock
__pycache__/
.venv/
.DS_Store
"""

ENV_TEXT = """\
# Secrets and settings for this project. Never commit this file.
# Each app folder has an env.template listing the names it needs.
"""

AGENTS_TEXT = """\
# AGENTS.md

This folder is a pdt project: a set of small scheduled jobs. Every folder holding a `config.yml` and a `run.py` is one Python app. Every folder holding a `config.yml` and one or more `.ps1` files but no `run.py` is one PowerShell app. `pdt.yml` holds the settings shared by every app.

## Working here

- Start a new app with `pdt new <name> --from <example>`; `pdt examples` lists the starting points. Do not copy an app folder by hand.
- An app declares its dependencies in the script header at the top of its `run.py`. Its `pdt-cli` pin is the pdt version the app runs with. Keep it at the newest pdt release, and run `pdt deploy <name>` after you raise it, so the deployed job uses it too.
- List the env vars an app reads under `env:` in its `config.yml`. Their values go in `.env`, which is never committed; `pdt deploy` uploads the ones that are set as cloud secrets. Each `$env:NAME` a PowerShell app's scripts read is required even when `config.yml` does not list it; list it under `env: optional:` when the scripts work without it. The same holds for each `os.environ["NAME"]` a Python app's `.py` files read; `os.environ.get("NAME")` and `os.getenv("NAME")` make it optional. pdt does not find a name that the code builds at run time.
- Check work with `pdt validate`, try it with `pdt run <name>`, ship it with `pdt deploy <name>`.
- Check a deployed app with `pdt health`, list its runs with `pdt runs <name> [--count 5] [--since 3d] [--span 1d]`, and read one run's log with `pdt logs <name> [N] [--count 5] [--since 3d] [--span 1d] --failed --errors` (`--id ID` picks a run by the id `pdt runs` shows; the last 20 lines; `--lines 50` for more, `--head` for the first lines, `--full` for all, `--follow` to wait for a running run's lines); add `--json` to any of them for machine-readable output. Stop a deployed app's schedule with `pdt pause <name>` (it writes `pause: true` into the app's `config.yml`), start it again with `pdt unpause <name>`, and start one run now with `pdt run <name> --deployed`. `pdt gui` shows the same in a browser.
- Read the files an app keeps in its data store with `pdt storage <name> ls [PATH] [--recursive] [--json]`, `pdt storage <name> get PATH`, and `pdt storage <name> query SQL`. When a run stopped but still holds the state lock, `pdt storage <name> unlock` releases it.
- Log with `log()` from `pdt.utils.log`; a plain `print()` also reaches the run's cloud logs, but without a severity.
- A PowerShell app reads its `config:` section from `$env:PDT_CONFIG_JSON | ConvertFrom-Json`, and pdt keeps each file it writes to `$env:PDT_OUTPUT_DIR` in the app's data store.
- A `run.ps1` file is the only entry script. Otherwise, `run_scripts` lists the entry scripts in order, or pdt runs each `.ps1` file that no other `.ps1` file loads in name order. Do not use `run.ps1` and `run_scripts` together.
- `pdt new <name> --from-scripts` writes `requirements.psd1` (the modules to install) and `run.py` (how the scripts run) into a PowerShell app's folder; once there, `requirements.psd1` replaces the modules pdt finds in the scripts, and `run.py` replaces how pdt runs them.
- An app folder holding a `Dockerfile` is built from that file instead of the generated one when it deploys to a cloud provider. The build context is the app folder under its own name next to `pdt.yml`; a `.dockerignore` in the app folder, with patterns relative to it, keeps files out of the image.
- A deploy to AWS, Azure, or Google Cloud builds the image with Docker on this computer when `docker info` answers, else in the cloud (ACR Tasks, Cloud Build, CodeBuild). `docker-build: local` or `docker-build: cloud` at the top level of `pdt.yml` or of an app's `config.yml` forces one place. A build with Docker forwards this computer's SSH agent, so a `RUN --mount=type=ssh` step in an app's `Dockerfile` can install a private git dependency. A build in the cloud has no agent, so it stops on such a step and names it.
"""

RUN_PY_TEXT = '''\
#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["pdt-cli[apps]==PDT_VERSION"]
# ///
"""`pdt new --from-scripts` wrote this file. It runs the .ps1 scripts in this
folder the way pdt runs an app with no run.py: each entry script in order
through pwsh with the app's `config:` section as JSON in PDT_CONFIG_JSON,
stopping at the first that fails, then it stores the files the scripts leave
in PDT_OUTPUT_DIR. When a script reads PDT_STATE_DIR, that folder holds the
app's state/ files and goes back to state/ when every script succeeds. Edit
it to change how they run.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from pdt import config, powershell
from pdt.pwsh import ensure_pwsh
from pdt.run_powershell import pwsh_command, reads_state
from pdt.utils import storage
from pdt.utils.log import die, log


def main() -> int:
    app_dir = Path(__file__).resolve().parent
    app = config.merged_app(app_dir.name)
    config.load_env(app_dir)
    missing = config.missing_env(app)
    if missing != "":
        die(1, f"env vars missing: {missing}")
    pwsh = ensure_pwsh()
    files = powershell.extract(app_dir)["files"]
    entries = powershell.split_files(files, app["run_scripts"])[0]
    store = storage.store() if app["storage"] else None
    code = 0
    with tempfile.TemporaryDirectory(prefix="pdt-run-") as local:
        output, state = Path(local) / "output", Path(local) / "state"
        output.mkdir()
        state.mkdir()
        lease = store.pull("state/", state) if store is not None and reads_state(files) else None
        env = {**os.environ, "PDT_OUTPUT_DIR": str(output), "PDT_STATE_DIR": str(state),
               "PDT_CONFIG_JSON": json.dumps(app["config"], default=str)}
        try:
            for script in entries:
                log("info", f"starting {script}")
                result = subprocess.run(
                    [pwsh, "-NoProfile", "-NonInteractive", "-Command", pwsh_command(app_dir / script)],
                    cwd=app_dir, env=env, stdin=subprocess.DEVNULL).returncode
                log("info", f"{script} ended", exit_code=result)
                if result != 0:
                    log("error", f"{script} failed", exit_code=result)
                    if code == 0:
                        code = result
                if result != 0 and not app["continue_on_error"]:
                    break
            kept = [file for file in output.rglob("*") if file.is_file()]
            if store is not None and kept:
                folder = store.run_folder() + "output/"
                store.push(output, folder)
                log("info", f"uploaded {len(kept)} output file(s) to {store.url}{folder}")
            if lease is not None and code == 0:
                store.push(state, "state/", lease)
                lease = None
        finally:
            if lease is not None:
                store.abort(lease)
    return code


if __name__ == "__main__":
    sys.exit(main())
'''

CLAUDE_TEXT = """\
@AGENTS.md
"""


def _ask(question: str, default: str = "") -> str:
    try:
        answer = console.ask(question, default)
    except EOFError:
        raise ConfigError(
            "there is no one to answer the questions. "
            "Run `pdt init` in a terminal, or add --yes to take the defaults.")
    return answer or default


def _ask_choice(question: str, labels: list[str], default: int = 1) -> int:
    console.say()
    console.heading(question)
    console.say()
    for number, label in enumerate(labels, start=1):
        console.choice(number, label)
    console.say()
    while True:
        answer = _ask("Choose", str(default))
        if answer.isdigit() and 1 <= int(answer) <= len(labels):
            return int(answer)
        console.warn(f"Please type a number from 1 to {len(labels)}.")


def bad_place(folder: Path) -> str:
    if folder == Path.home().resolve():
        return "This is your home folder. Putting a project here mixes it with everything else."
    if folder.parent == folder:
        return "This is the top of the disk."
    text = str(folder)
    for system in (*SYSTEM_FOLDERS, tempfile.gettempdir()):
        if text == system or text.startswith(system + os.sep):
            return "This folder belongs to the operating system."
    return ""


def choose_target(requested: str | None, assume_yes: bool) -> Path:
    if requested is not None:
        folder = Path(requested).expanduser().resolve()
        if folder.exists() and not folder.is_dir():
            raise ConfigError(f"{folder} is a file, not a folder.")
        return folder

    here = Path.cwd().resolve()
    if assume_yes:
        return here

    warning = bad_place(here)
    console.say()
    console.field("This folder", str(here))
    if warning != "":
        console.warn(f"Careful: {console.escape(warning)}")

    labels = ["Make a new folder inside this one", f"Use this folder ({here.name})", "Cancel"]
    default = 1 if warning != "" else 2
    choice = _ask_choice("Where should the project live?", labels, default)
    if choice == 3:
        raise ConfigError("cancelled")
    if choice == 2:
        return here
    while True:
        name = _ask("Name for the new folder", "pdt-jobs")
        folder = (here / name).resolve()
        if not folder.exists():
            return folder
        console.warn(f"{console.value(folder)} already exists. Pick another name.")


def ask_provider(current: str = "") -> str:
    labels = [label for _key, label in PROVIDER_CHOICES]
    keys = [key for key, _label in PROVIDER_CHOICES]
    choice = _ask_choice("Where should your jobs run?", labels,
                         keys.index(current) + 1 if current != "" else 1)
    return keys[choice - 1]


def kept_settings(current: dict, provider: str) -> dict:
    if current.get("provider") == provider:
        return dict(current)
    timezone = current.get("timezone")
    if timezone not in (None, "local") and TIMEZONE_QUESTION in PROVIDER_QUESTIONS[provider]:
        return {"timezone": timezone}
    return {}


def ask_settings(provider: str, kept: dict) -> dict | None:
    console.say()
    settings = {"provider": provider, **kept}
    for key, question, default, check in PROVIDER_QUESTIONS[provider]:
        while True:
            answer = _ask(f"{question} (type cancel to change nothing)",
                          str(kept.get(key) or default()))
            if answer.strip().lower() == "cancel":
                return None
            problem = check(answer)
            if problem == "":
                break
            console.warn(console.escape(problem))
        settings[key] = answer
    return settings


def ask_platform(assume_yes: bool) -> dict:
    if assume_yes:
        return {}
    provider = ask_provider()
    if provider == "":
        return {}
    settings = ask_settings(provider, {})
    if settings is None:
        raise ConfigError("cancelled")
    return settings


def platform_lines(platform: dict) -> list[str]:
    return ["platform:", *(f"  {key}: {config.yaml_quoted(value)}" for key, value in platform.items()
                           if value is not None)]


def replace_platform(text: str, platform: dict) -> str:
    import yaml
    end_of_line = "\r" if "\r\n" in text else ""
    lines = text.split("\n")
    new = [line + end_of_line for line in platform_lines(platform)]
    root = yaml.compose(text)
    events = list(yaml.parse(text))
    if isinstance(root, yaml.MappingNode) and root.flow_style:
        raise ConfigError(f"the top level of {PROJECT_FILE} is in braces, so pdt cannot change "
                          f"it safely. Change platform: in {PROJECT_FILE} by hand.")
    if any(isinstance(event, yaml.DocumentEndEvent) and event.explicit for event in events):
        raise ConfigError(f"{PROJECT_FILE} has a ... document end line, so pdt cannot change it "
                          f"safely. Change platform: in {PROJECT_FILE} by hand.")
    keys = [key for key, _value in (root.value if root is not None else [])]
    found = [(key, value) for key, value in (root.value if root is not None else [])
             if key.value == "platform"]
    if found:
        key, value = found[0]
        following = [other.start_mark.line for other in keys if other.start_mark.line > key.start_mark.line]
        bound = min(following, default=len(lines))
        if isinstance(value, yaml.MappingNode) and not value.flow_style and value.value:
            last = value.value[-1][1].end_mark
        else:
            last = value.end_mark
        start, stop = key.start_mark.line, last.line + (last.column > 0)
        if any(getattr(event, "anchor", None) and start <= event.start_mark.line < bound
               for event in events):
            raise ConfigError(f"platform: in {PROJECT_FILE} holds a YAML anchor (&) or alias (*), "
                              f"so pdt cannot change it safely. Change platform: in {PROJECT_FILE} "
                              "by hand.")
        lines[start:stop] = new
    elif PLATFORM_TEMPLATE + end_of_line in lines:
        start = lines.index(PLATFORM_TEMPLATE + end_of_line)
        stop = start + 1
        while stop < len(lines) and lines[stop].startswith("#  "):
            stop += 1
        lines[start:stop] = new
    else:
        if lines[-1] != "":
            lines[-1] += end_of_line
            lines.append("")
        lines[-1:] = [end_of_line, *new, ""]
    return "\n".join(lines)


def merged_platform(root_cfg: dict, root_platform: dict, app_dir: Path) -> dict:
    return {**root_platform,
            **(config.root_app_entry(root_cfg, app_dir.name).get("platform") or {}),
            **(config.load_yaml(app_dir / APP_FILE).get("platform") or {})}


def pin_problem(path: Path, shown: str) -> str:
    import yaml
    text = path.read_text(encoding="utf-8-sig") if path.is_file() else ""
    root = yaml.compose(text)
    if isinstance(root, yaml.MappingNode) and root.flow_style:
        return (f"the top level of {shown} is in braces, so pdt cannot add platform: to it. "
                f"Write each key of {shown} on its own line, or delete the {{}} when {shown} holds "
                "nothing else.")
    if (isinstance(root, yaml.MappingNode) and any(key.value == "platform" for key, _value in root.value)
            and not any(line.strip() == "platform:" for line in text.splitlines())):
        return (f"platform: in {shown} is not on a line of its own, so pdt cannot change it. "
                f"Write platform: on its own line in {shown}, and each key under it on its own "
                "indented line.")
    return ""


def ask_apps(numbered: list[str]) -> list[str]:
    while True:
        answer = _ask("Type the numbers of the apps to move, separated by spaces "
                      "(Enter cancels)")
        numbers = answer.replace(",", " ").split()
        if all(number.isdigit() and 1 <= int(number) <= len(numbered) for number in numbers):
            return [name for index, name in enumerate(numbered, start=1)
                    if str(index) in numbers]
        console.warn(f"Please type numbers from 1 to {console.value(len(numbered))}.")


def change_provider(target: Path) -> int:
    marker = target / PROJECT_FILE
    text = marker.read_bytes().decode("utf-8")
    bom = "\ufeff" if text.startswith("\ufeff") else ""
    root_cfg = config.load_yaml(marker)
    current = root_cfg.get("platform") or {}
    old = str(current.get("provider") or "")
    console.say(f"{console.value(target)} is already a pdt project.")
    if old != "":
        console.field("Runs on", NAMES.get(old, old))
    deployed = [name for name in config.read_state().get("deployed") or []
                if name in config.app_folders()]
    apps = {name: config.merged_app(name) for name in deployed}
    numbered = [name for name in deployed
                if "provider" not in merged_platform(root_cfg, {}, target / name)]
    before = {name: deploy.resolved(apps[name]["platform"]) for name in deployed}
    recorded = {name: config.deployed_platform(name) or before[name] for name in deployed}
    where = {name: deploy.describe_place(config.place(recorded[name])) for name in deployed}
    if deployed:
        console.say()
        console.table(["", "app", "where"],
                      [[str(numbered.index(name) + 1), name, where[name]] if name in numbered
                       else ["", name, f"{where[name]}; edit {apps[name]['platform_from']} "
                                       "to move it"]
                       for name in deployed])
    provider = ask_provider(old if old in config.PROVIDERS else "")
    if provider == "":
        console.say("Nothing changed.")
        return 0
    platform = ask_settings(provider, kept_settings(current, provider))
    if platform is None:
        console.say("Cancelled. Nothing changed.")
        return 0
    changed = bom + replace_platform(text.removeprefix(bom), platform)
    there = deploy.describe_place(config.place(deploy.resolved(platform)))
    after = {name: merged_platform(root_cfg, platform, target / name) for name in deployed}
    pins = {}
    for name in deployed:
        settings = {**config.place(before[name]),
                    **{key: str(before[name][key]) for key in ("profile", "timezone")
                       if before[name].get(key)}}
        pins[name] = {key: value for key, value in settings.items()
                      if str(deploy.resolved(after[name]).get(key) or "") != value}
        problem = pin_problem(target / name / APP_FILE, f"{name}/{APP_FILE}")
        if pins[name].keys() - {"timezone"} and problem != "":
            raise ConfigError(problem)
    movable = [name for name in numbered if deploy.moved(recorded[name], after[name])]
    moving = []
    if movable:
        here = deploy.describe_place(config.place(deploy.resolved(current)))
        choice = _ask_choice(f"What should happen to the apps deployed on {console.escape(here)}?",
                             ["Move all", "Move some",
                              f"Keep them on {here} (only new apps go to {there})", "Cancel"], 4)
        if choice == 1:
            moving = movable
        elif choice == 2:
            moving = [name for name in ask_apps(numbered) if name in movable]
        if choice == 4 or choice == 2 and not moving:
            console.say("Cancelled. Nothing changed.")
            return 0
    if moving:
        console.say()
        console.say(f"pdt init runs {console.value('pdt destroy')} for "
                    f"{', '.join(console.value(name) for name in moving)}, "
                    f"then saves {console.value(PROJECT_FILE)}.")
        console.say(f"{console.value('pdt destroy')} keeps each app's stored data.")
        try:
            answer = console.confirm("Remove them?", choices="[y = remove and continue, N = cancel]")
        except EOFError:
            answer = False
        if not answer:
            console.say("Cancelled. Nothing changed.")
            return 0
    removed, pinned = [], []
    for name in moving:
        console.say()
        if deploy.destroy(name, assume_yes=True) != 0:
            console.error(f"{console.value(name)} was not removed. "
                          f"It is still deployed on {console.value(where[name])}.")
            break
        removed.append(name)
    console.say()
    for name in deployed:
        if name in removed or not pins[name].keys() - {"timezone"}:
            continue
        for key, value in pins[name].items():
            config.save_platform_key(apps[name], key, value, path=target / name / APP_FILE)
        pinned.append(name)
        console.say(f"{console.value(name)} stays on "
                    f"{console.value(deploy.describe_place(config.place(before[name])))}. "
                    f"Its {console.value('platform:')} settings are in "
                    f"{console.value(f'{name}/{APP_FILE}')}.")
    with config.locked(marker):
        if marker.read_bytes().decode("utf-8") != text:
            raise ConfigError(f"{PROJECT_FILE} changed while pdt init was asking; "
                              "run pdt init again")
        config.write_text_atomically(marker, changed, newline="")
    console.done(f"Saved the {console.value('platform:')} settings to {console.value(PROJECT_FILE)}.")
    console.say()
    if removed:
        console.next_steps([(f"pdt deploy {name}", f"deploy it to {console.escape(there)}")
                            for name in removed])
    elif not moving and not pinned:
        console.next_steps([("pdt validate", "check every app on the new settings"),
                            ("pdt deploy --all", f"deploy every app to {NAMES[provider]}")])
    return 0 if removed == moving else 1


def project_yaml(platform: dict) -> str:
    lines = [
        "# This file marks the top of your pdt project.",
        "# Every app folder next to this file is a job pdt can run and deploy.",
        "",
    ]
    if platform:
        lines += [
            "# Defaults for every app. An app's own config.yml can override them.",
            "platform:",
        ]
        lines += [f"  {key}: {value}" for key, value in platform.items()]
    else:
        lines += [
            "# Defaults for every app. Fill this in before you deploy.",
            PLATFORM_TEMPLATE,
            "#  provider: azure        # azure, aws, google-cloud, or windows",
            "#  region: eastus2",
        ]
    lines += [
        "",
        "# Settings for one app. The app's own config.yml can hold these instead.",
        "apps: []",
        "",
    ]
    return "\n".join(lines)


def has_nothing_in_it(folder: Path) -> bool:
    if not folder.exists():
        return True
    return not any(child for child in folder.iterdir() if not child.name.startswith("."))


def init(directory: str | None, assume_yes: bool) -> int:
    console.art(ART)
    here = Path.cwd().resolve()
    if directory is None and (here / PROJECT_FILE).is_file():
        target = here
    else:
        target = choose_target(directory, assume_yes)
    marker = target / PROJECT_FILE
    if marker.is_file():
        if assume_yes:
            console.say(f"{console.value(target)} is already a pdt project.")
            return 0
        previous = os.environ.get("PDT_PROJECT")
        os.environ["PDT_PROJECT"] = str(target)
        try:
            return change_provider(target)
        finally:
            if previous is None:
                del os.environ["PDT_PROJECT"]
            else:
                os.environ["PDT_PROJECT"] = previous
    starting_fresh = has_nothing_in_it(target)

    platform = ask_platform(assume_yes)
    target.mkdir(parents=True, exist_ok=True)
    marker.write_text(project_yaml(platform))
    for name, body in ((".gitignore", GITIGNORE_TEXT), (".env", ENV_TEXT),
                       ("AGENTS.md", AGENTS_TEXT), ("CLAUDE.md", CLAUDE_TEXT)):
        path = target / name
        if not path.exists():
            path.write_text(body)
    private_file(target / ".env")
    if starting_fresh:
        copy_example(target, STARTER, EXAMPLES / STARTER)

    console.done(f"Your project is ready: {console.value(target)}")
    files = [(PROJECT_FILE, "settings shared by every app"), (".env", "secrets, never committed"),
             (".gitignore", ""),
             ("AGENTS.md", "how an AI agent should work in this project "
                           f"({console.value('CLAUDE.md')} points here)")]
    if starting_fresh:
        files.append((f"{STARTER}/", "a working app to run and edit"))
    console.columns(files)
    console.say()
    steps = []
    if target != here:
        steps.append((f"cd {target.name}", "go to the new project"))
    if starting_fresh:
        steps.append((f"pdt run {STARTER}", "run the starter app on this computer"))
        if platform:
            steps.append((f"pdt deploy {STARTER}", "run it on a schedule on "
                          f"{NAMES[platform['provider']]}"))
    steps.append(("pdt examples", "see what else you can start from"))
    console.next_steps(steps)
    console.say()
    console.say(f"{watch.COMMUNITY} {console.value(watch.SUPPORT_URL)}")
    return 0


def welcome() -> int:
    console.art(ART)
    console.next_steps([("pdt init my-project", "create a project in a new folder"),
                        ("cd my-project", "go to the new project"),
                        (f"pdt run {STARTER}", "run the starter app on this computer"),
                        ("pdt --help", "see every command")], "Get started:")
    console.say()
    console.say(f"{watch.COMMUNITY} {console.value(watch.SUPPORT_URL)}")
    return 0


def examples() -> list[Path]:
    return sorted(child for child in EXAMPLES.iterdir() if child.is_dir())


def copy_example(root: Path, name: str, example: Path) -> None:
    destination = root / name
    shutil.copytree(example, destination,
                    ignore=shutil.ignore_patterns("__pycache__", ".env"))
    run_py = destination / "run.py"
    if run_py.is_file():
        run_py.write_text(run_py.read_text().replace("PDT_VERSION", __version__))


def summary_of(example: Path) -> str:
    """The comment block at the top of the example's config, as one line."""
    summary = []
    for line in (example / APP_FILE).read_text().splitlines():
        if not line.startswith("#"):
            break
        summary.append(line[1:].strip())
    return " ".join(summary).strip()


def print_examples() -> None:
    for example in examples():
        console.name(example.name)
        console.detail(console.escape(summary_of(example)))
        console.say()


def list_examples() -> int:
    console.heading("Example apps you can start from:")
    console.say()
    print_examples()
    console.say(f"Copy one with:  {console.value('pdt new <name> --from <example>')}")
    return 0


def new_app(name: str, source: str | None) -> int:
    problem = app_name_problem(name)
    if problem != "":
        raise ConfigError(problem)
    root = find_project()
    destination = root / name
    if destination.exists():
        raise ConfigError(f"{destination} already exists.")
    example = EXAMPLES / (source or STARTER)
    if example not in examples():
        raise ConfigError(
            f"there is no example named {source!r}. Run `pdt examples` to see them.")
    copy_example(root, name, example)
    console.done(f"Created {console.value(f'{name}/')} from the {console.value(example.name)} "
                 "example.")
    files = [(f"{name}/{job}", "the job itself") for job in powershell_scripts(destination) or ["run.py"]]
    files.append((f"{name}/config.yml", "how often it runs and what it needs"))
    needs_secrets = (destination / "env.template").is_file()
    if needs_secrets:
        files.append((f"{name}/env.template", f"the secrets to copy into {console.value('.env')}"))
    console.columns(files)
    console.say()
    if needs_secrets:
        console.styled(f"Open {console.value(f'{name}/env.template')} and copy the names you need "
                       f"into {console.value('.env')}.")
    console.next_steps([("pdt validate", f"check the config and the {console.value('.env')} values"),
                        (f"pdt run {name}", "run the app on this computer")])
    return 0


def from_scripts(name: str) -> int:
    problem = app_name_problem(name)
    if problem != "":
        raise ConfigError(problem)
    folder = find_project() / name
    if not folder.is_dir():
        raise ConfigError(f"there is no folder {name}/ in the project {folder.parent}.")
    for file in ("run.py", "requirements.psd1"):
        if (folder / file).exists():
            raise ConfigError(f"{name}/{file} already exists. Delete it first to write a new one.")
    if not powershell_scripts(folder):
        raise ConfigError(f"{name}/ holds no .ps1 file, so there is nothing to write files for.")
    app = merged_app(name)
    try:
        modules = powershell.scan(app, app["platform"].get("provider", "")).modules
        latest = powershell.latest_versions([m.name for m in modules if m.version is None])
    except (pwsh.PwshError, powershell.PowerShellError) as e:
        raise ConfigError(str(e))
    lines = [f"# pdt installs these modules before the scripts run. "
             f"`pdt new {name} --from-scripts` wrote this file.", "@{"]
    for m in modules:
        version = m.version or latest[m.name]
        lines.append(f"    {powershell.quoted(m.name)} = {powershell.quoted(version)}")
    (folder / "requirements.psd1").write_text("\n".join(lines + ["}", ""]))
    (folder / "run.py").write_text(RUN_PY_TEXT.replace("PDT_VERSION", __version__))
    console.done(f"Wrote {console.value('requirements.psd1')} and {console.value('run.py')} for the "
                 f"scripts in {console.value(f'{name}/')}.")
    console.columns([(f"{name}/requirements.psd1", "the modules pdt installs before the scripts run"),
                     (f"{name}/run.py", "how the scripts run")])
    console.say()
    console.next_steps([("pdt validate", f"check the config and the {console.value('.env')} values"),
                        (f"pdt run {name}", "run the scripts on this computer")])
    return 0
