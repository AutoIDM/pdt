"""Create a project (`pdt init`) and add apps to it (`pdt new`)."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from pdt import __version__
from pdt.config import APP_FILE, PROJECT_FILE, ConfigError, find_project

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
PROVIDER_QUESTIONS = {
    "azure": [
        ("region", "Which Azure region should hold your jobs?", "eastus2", _needed),
    ],
    "aws": [
        ("region", "Which AWS region should hold your jobs?", "us-east-1", _needed),
    ],
    "google-cloud": [
        ("region", "Which Google Cloud region should hold your jobs?", "us-central1", _needed),
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
__pycache__/
.venv/
.DS_Store
"""

ENV_TEXT = """\
# Secrets and settings for this project. Never commit this file.
# Each app folder has an env.template listing the names it needs.
"""


def _ask(question: str, default: str = "") -> str:
    suffix = f" [{default}]" if default != "" else ""
    try:
        answer = input(f"{question}{suffix}: ").strip()
    except EOFError:
        raise ConfigError(
            "there is no one to answer the questions. "
            "Run `pdt init` in a terminal, or add --yes to take the defaults.")
    return answer or default


def _ask_choice(question: str, labels: list[str], default: int = 1) -> int:
    print()
    print(question)
    print()
    for number, label in enumerate(labels, start=1):
        print(f"  {number}) {label}")
    print()
    while True:
        answer = _ask("Choose", str(default))
        if answer.isdigit() and 1 <= int(answer) <= len(labels):
            return int(answer)
        print(f"Please type a number from 1 to {len(labels)}.")


def bad_place(folder: Path) -> str:
    if folder == Path.home().resolve():
        return "This is your home folder. Putting a project here mixes it with everything else."
    if folder.parent == folder:
        return "This is the top of the disk."
    text = str(folder)
    for system in SYSTEM_FOLDERS:
        if text == system or text.startswith(system + os.sep):
            return "This folder belongs to the operating system."
    return ""


def choose_target(requested: str | None, assume_yes: bool) -> Path:
    if requested is not None:
        folder = Path(requested).expanduser().resolve()
        if folder.exists():
            raise ConfigError(f"{folder} already exists. Pick a name that is not taken.")
        return folder

    here = Path.cwd().resolve()
    if assume_yes:
        return here

    warning = bad_place(here)
    print()
    print(f"This folder: {here}")
    if warning != "":
        print(f"Careful: {warning}")

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
        print(f"{folder} already exists. Pick another name.")


def ask_platform(assume_yes: bool) -> dict:
    if assume_yes:
        return {}
    labels = [label for _key, label in PROVIDER_CHOICES]
    choice = _ask_choice("Where should your jobs run?", labels, 1)
    provider = PROVIDER_CHOICES[choice - 1][0]
    if provider == "":
        return {}
    print()
    settings = {"provider": provider}
    for key, question, default, check in PROVIDER_QUESTIONS[provider]:
        while True:
            answer = _ask(question, default)
            problem = check(answer)
            if problem == "":
                break
            print(problem)
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
        print(f"{target} is already a pdt project.")
        return 0
    starting_fresh = has_nothing_in_it(target)

    platform = ask_platform(assume_yes)
    target.mkdir(parents=True, exist_ok=True)
    marker.write_text(project_yaml(platform))
    for name, body in ((".gitignore", GITIGNORE_TEXT), (".env", ENV_TEXT)):
        path = target / name
        if not path.exists():
            path.write_text(body)
    if starting_fresh:
        copy_example(target, STARTER, EXAMPLES / STARTER)

    print()
    print(f"Your project is ready: {target}")
    print(f"  {PROJECT_FILE}   settings shared by every app")
    print("  .env      secrets, never committed")
    print("  .gitignore")
    if starting_fresh:
        print(f"  {STARTER}/  a working app to run and edit")
    print()
    print("Next steps:")
    if target != Path.cwd().resolve():
        print(f"  cd {target.name}")
    if starting_fresh:
        print(f"  pdt run {STARTER}")
    print("  pdt examples    see what else you can start from")
    return 0


def examples() -> list[Path]:
    return sorted(child for child in EXAMPLES.iterdir() if child.is_dir())


def copy_example(root: Path, name: str, example: Path) -> None:
    destination = root / name
    shutil.copytree(example, destination,
                    ignore=shutil.ignore_patterns("__pycache__", ".env"))
    run_py = destination / "run.py"
    run_py.write_text(run_py.read_text().replace("PDT_VERSION", __version__))


def summary_of(example: Path) -> str:
    for line in (example / APP_FILE).read_text().splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return ""


def list_examples() -> int:
    for example in examples():
        print(f"{example.name}  {summary_of(example)}".rstrip())
    return 0


def new_app(name: str, source: str | None) -> int:
    root = find_project()
    destination = root / name
    if destination.exists():
        raise ConfigError(f"{destination} already exists.")
    if source is None:
        print("Pick the example to start from:")
        for example in examples():
            print(f"  {example.name}  {summary_of(example)}".rstrip())
        print()
        raise ConfigError(f"say which one, for example `pdt new {name} --from {examples()[0].name}`")
    example = EXAMPLES / source
    if example not in examples():
        raise ConfigError(
            f"there is no example named {source!r}. Run `pdt examples` to see them.")
    copy_example(root, name, example)
    print(f"Created {name}/ from the {example.name} example.")
    print(f"  {name}/run.py       the job itself")
    print(f"  {name}/config.yml   how often it runs and what it needs")
    needs_secrets = (destination / "env.template").is_file()
    if needs_secrets:
        print(f"  {name}/env.template the secrets to copy into .env")
    print()
    print("Next steps:")
    if needs_secrets:
        print(f"  open {name}/env.template and copy the names you need into .env")
    print("  pdt validate")
    print(f"  pdt run {name}")
    return 0
