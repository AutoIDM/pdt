"""Create a project (`pdt init`) and add apps to it (`pdt new`)."""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

from pdt import __version__, console, powershell, pwsh, regions
from pdt.config import (APP_FILE, PROJECT_FILE, ConfigError, app_name_problem, find_project,
                        merged_app, powershell_scripts)
from pdt.utils.env_secret import private_file

EXAMPLES = Path(__file__).resolve().parent / "examples"
STARTER = "hello-world"

PROVIDER_CHOICES = [
    ("azure", "Microsoft Azure"),
    ("aws", "Amazon Web Services"),
    ("google-cloud", "Google Cloud"),
    ("windows", "This Windows PC, using Task Scheduler"),
    ("", "Decide later"),
]


def _needed(answer: str) -> str:
    return "" if answer.strip() != "" else "This one is needed. Please type a value."


# Ask only what pdt cannot supply. Every provider learns its own account,
# subscription, or project from the credentials at deploy time and writes the
# answer back, so region is the only thing left that the user must choose.
# Each default is a function, so the region suggested from this computer's
# time zone is read only when the question is asked.
PROVIDER_QUESTIONS = {
    "azure": [
        ("region", regions.question("azure"), lambda: regions.suggest_region("azure"), _needed),
    ],
    "aws": [
        ("region", regions.question("aws"), lambda: regions.suggest_region("aws"), _needed),
    ],
    "google-cloud": [
        ("region", regions.question("google-cloud"),
         lambda: regions.suggest_region("google-cloud"), _needed),
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
- Check a deployed app with `pdt health`, list its runs with `pdt runs <name> [--count 5] [--since 3d] [--span 1d]`, and read one run's log with `pdt logs <name> [N] [--count 5] [--since 3d] [--span 1d] --failed --errors` (the last 20 lines; `--lines 50` for more, `--head` for the first lines, `--full` for all, `--follow` to wait for a running run's lines); add `--json` to any of them for machine-readable output.
- Log with `log()` from `pdt.utils.log`; a plain `print()` also reaches the run's cloud logs, but without a severity.
- A `run.ps1` file is the only entry script. Otherwise, `run_scripts` lists the entry scripts in order, or pdt runs each `.ps1` file that no other `.ps1` file loads in name order. Do not use `run.ps1` and `run_scripts` together.
- `pdt new <name> --from-scripts` writes `requirements.psd1` (the modules to install) and `run.py` (how the scripts run) into a PowerShell app's folder; once there, `requirements.psd1` replaces the modules pdt finds in the scripts, and `run.py` replaces how pdt runs them.
- An app folder holding a `Dockerfile` is built from that file instead of the generated one when it deploys to a cloud provider. The build context is the app folder under its own name next to `pdt.yml`; a `.dockerignore` in the app folder, with patterns relative to it, keeps files out of the image.
"""

RUN_PY_TEXT = '''\
#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["pdt-cli[apps]==PDT_VERSION"]
# ///
"""`pdt new --from-scripts` wrote this file. It runs the .ps1 scripts in this
folder the way pdt runs an app with no run.py: each entry script in order
through pwsh, stopping at the first that fails, then it stores the files the
scripts leave in PDT_OUTPUT_DIR. Edit it to change how they run.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

from pdt import config, powershell
from pdt.pwsh import ensure_pwsh
from pdt.run_powershell import pwsh_command
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
    entries = powershell.split_files(
        powershell.extract(app_dir)["files"], app["run_scripts"])[0]
    code = 0
    with tempfile.TemporaryDirectory(prefix="pdt-output-") as output:
        env = {**os.environ, "PDT_OUTPUT_DIR": output}
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
        files = [file for file in Path(output).rglob("*") if file.is_file()]
        if app["storage"] and files:
            store = storage.store()
            folder = store.run_folder() + "output/"
            store.push(Path(output), folder)
            log("info", f"uploaded {len(files)} output file(s) to {store.url}{folder}")
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
        console.warn(f"Careful: {warning}")

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
        console.warn(f"{folder} already exists. Pick another name.")


def ask_platform(assume_yes: bool) -> dict:
    if assume_yes:
        return {}
    labels = [label for _key, label in PROVIDER_CHOICES]
    choice = _ask_choice("Where should your jobs run?", labels, 1)
    provider = PROVIDER_CHOICES[choice - 1][0]
    if provider == "":
        return {}
    console.say()
    settings = {"provider": provider}
    for key, question, default, check in PROVIDER_QUESTIONS[provider]:
        while True:
            answer = _ask(question, default())
            problem = check(answer)
            if problem == "":
                break
            console.warn(problem)
        settings[key] = answer
    return settings


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
            "#platform:",
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
    target = choose_target(directory, assume_yes)
    marker = target / PROJECT_FILE
    if marker.is_file():
        console.say(f"{target} is already a pdt project.")
        return 0
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

    console.say()
    console.done(f"Your project is ready: {target}")
    files = [(PROJECT_FILE, "settings shared by every app"), (".env", "secrets, never committed"),
             (".gitignore", ""),
             ("AGENTS.md", "how an AI agent should work in this project "
                           f"({console.value('CLAUDE.md')} points here)")]
    if starting_fresh:
        files.append((f"{STARTER}/", "a working app to run and edit"))
    console.columns(files)
    console.say()
    steps = []
    if target != Path.cwd().resolve():
        steps.append((f"cd {target.name}", "go to the new project"))
    if starting_fresh:
        steps.append((f"pdt run {STARTER}", "run the starter app on this computer"))
    steps.append(("pdt examples", "see what else you can start from"))
    console.next_steps(steps)
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
    console.styled("Copy one with:  [bold]pdt new <name> --from <example>[/]")
    return 0


def new_app(name: str, source: str | None) -> int:
    problem = app_name_problem(name)
    if problem != "":
        raise ConfigError(problem)
    root = find_project()
    destination = root / name
    if destination.exists():
        raise ConfigError(f"{destination} already exists.")
    if source is None:
        console.heading("Pick the example to start from:")
        console.say()
        print_examples()
        raise ConfigError(f"say which one, for example `pdt new {name} --from {examples()[0].name}`")
    example = EXAMPLES / source
    if example not in examples():
        raise ConfigError(
            f"there is no example named {source!r}. Run `pdt examples` to see them.")
    copy_example(root, name, example)
    console.done(f"Created {name}/ from the {example.name} example.")
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
    console.done(f"Wrote requirements.psd1 and run.py for the scripts in {name}/.")
    console.columns([(f"{name}/requirements.psd1", "the modules pdt installs before the scripts run"),
                     (f"{name}/run.py", "how the scripts run")])
    console.say()
    console.next_steps([("pdt validate", f"check the config and the {console.value('.env')} values"),
                        (f"pdt run {name}", "run the scripts on this computer")])
    return 0
