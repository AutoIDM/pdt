"""`pdt migrate`: update a project that an older pdt release set up.

Each check looks for one thing a later release changed. It returns a
Finding for each place in the project that still has the old form. A
finding with `apply` is a change pdt makes after the user agrees; one
without it is a step the user must take. Every check reads the files
as they are, so a second run finds nothing that the first run fixed.

After the changes, `pdt validate` runs, and whatever it still reports
is left for the user.
"""

from __future__ import annotations

import dataclasses
import hashlib
import re
from pathlib import Path
from typing import Callable, Iterator

from dotenv import dotenv_values

from pdt import __version__, config, console, deploy, scaffold
from pdt.config import APP_FILE, PROJECT_FILE
from pdt.deploy_common import DOCKERFILE

# The sha256 of every AGENTS.md text an earlier pdt wrote. A project whose
# AGENTS.md still matches one was never edited, so migrate replaces it.
SHIPPED_AGENTS = {
    "bafca438fb721383ae7cc3931407bb9e4f2e1a914aad1c9f6a16646806d5a0c9",
    "22527471ebfcbdde9f1a6efee6bbf0beeeda077931c9cd11a117baf501ff8bf9",
}
OLD_ENTRYPOINT = 'ENTRYPOINT ["uv", "run", "--script", "run.py"]'
ENTRYPOINT = next(line for line in DOCKERFILE.splitlines() if line.startswith("ENTRYPOINT"))
OLD_RUNTIMES = {"lambda": "AWS Lambda function", "functions": "Azure Function App"}
ENVIRONMENT_LINE = re.compile(
    r"^(\s*(?:export\s+)?PDT_AZURE_CONTAINER_APPS_ENVIRONMENT\s*=\s*)(['\"]?)([^'\"#\s]*)\2(.*)$")


@dataclasses.dataclass(frozen=True)
class Finding:
    path: Path
    what: str
    apply: Callable[[], None] | None = None


def parent_key(lines: list[str], index: int) -> str:
    """The key of the mapping that holds lines[index], by indentation."""
    indent = len(lines[index]) - len(lines[index].lstrip())
    for line in reversed(lines[:index]):
        text = line.strip()
        if text == "" or text.startswith("#"):
            continue
        if len(line) - len(line.lstrip()) < indent:
            return text.removeprefix("- ").partition(":")[0].strip()
    return ""


def write(path: Path, text: str) -> Callable[[], None]:
    return lambda: path.write_text(text)


def retired_runtime(project: Path) -> Iterator[Finding]:
    """0.1.1: every cloud job runs as a container, so `platform.runtime` went away."""
    apps = config.app_folders()
    for path in [project / PROJECT_FILE, *(project / name / APP_FILE for name in apps)]:
        if not path.is_file():
            continue
        lines = path.read_text().splitlines(keepends=True)
        runtime = [i for i, line in enumerate(lines)
                   if line.strip().startswith("runtime:") and parent_key(lines, i) == "platform"]
        if not runtime:
            continue
        yield Finding(path, "remove `runtime:` from platform; pdt no longer uses it",
                      write(path, "".join(line for i, line in enumerate(lines) if i not in runtime)))
        for i in runtime:
            value = lines[i].partition(":")[2].split("#")[0].strip(" '\"\n")
            if value in OLD_RUNTIMES:
                yield Finding(path, f"if pdt 0.1.0 deployed this app, its {OLD_RUNTIMES[value]} "
                                    "is still there. Remove it with "
                                    "`uvx --from pdt-cli==0.1.0 pdt destroy "
                                    "<app>`, then run `pdt deploy <app>`")


def azure_environment(project: Path) -> Iterator[Finding]:
    """0.1.2: PDT_AZURE_CONTAINER_APPS_ENVIRONMENT names <resource-group>/<name>."""
    platform = config.load_yaml(project / PROJECT_FILE).get("platform") or {}
    for path in [project / ".env", *(project / name / ".env" for name in config.app_folders())]:
        if not path.is_file():
            continue
        group = (dotenv_values(path).get("PDT_AZURE_RESOURCE_GROUP")
                 or platform.get("resource_group") or "pdt")
        lines = path.read_text().splitlines(keepends=True)
        names = []
        for i, line in enumerate(lines):
            match = ENVIRONMENT_LINE.match(line.rstrip("\n"))
            if match is None or match[3] == "" or "/" in match[3]:
                continue
            names.append(match[3])
            lines[i] = f"{match[1]}{match[2]}{group}/{match[3]}{match[2]}{match[4]}\n"
        if names:
            yield Finding(path, f"write PDT_AZURE_CONTAINER_APPS_ENVIRONMENT as "
                                f"{group}/{names[0]}, its resource group and name",
                          write(path, "".join(lines)))


def dockerfile_exit_line(project: Path) -> Iterator[Finding]:
    """0.1.3: a cloud job's log ends with `pdt: exit N`, which `pdt runs` reads."""
    for name in config.app_folders():
        path = project / name / "Dockerfile"
        if not path.is_file():
            continue
        text = path.read_text()
        if "pdt: exit" in text:
            continue
        lines = text.splitlines(keepends=True)
        old = [i for i, line in enumerate(lines) if " ".join(line.split()) == OLD_ENTRYPOINT]
        if old:
            new_text = "".join(ENTRYPOINT + "\n" if i in old else line
                               for i, line in enumerate(lines))
            yield Finding(path, "use the ENTRYPOINT that reports each run's exit code to "
                                f"`pdt runs`; run `pdt deploy {name}` afterwards",
                          write(path, new_text))
        else:
            yield Finding(path, "the job must print `pdt: exit <code>` as its last log line, "
                                "or `pdt runs` cannot tell whether a run worked. Copy the "
                                f"ENTRYPOINT line pdt uses: {ENTRYPOINT}")


def project_files(project: Path) -> Iterator[Finding]:
    """0.1.1 and later: the files `pdt init` writes for an AI agent and for git."""
    agents = project / "AGENTS.md"
    if not agents.exists():
        yield Finding(agents, "create it, to tell an AI agent how to work in this project",
                      write(agents, scaffold.AGENTS_TEXT))
    elif hashlib.sha256(agents.read_text().encode()).hexdigest() in SHIPPED_AGENTS:
        yield Finding(agents, "replace it with the text for this pdt release",
                      write(agents, scaffold.AGENTS_TEXT))
    claude = project / "CLAUDE.md"
    if not claude.exists():
        yield Finding(claude, "create it, pointing Claude Code at AGENTS.md",
                      write(claude, scaffold.CLAUDE_TEXT))
    ignore = project / ".gitignore"
    text = ignore.read_text() if ignore.is_file() else ""
    have = {line.strip() for line in text.splitlines()}
    missing = [line for line in scaffold.GITIGNORE_TEXT.splitlines() if line not in have]
    if missing:
        joined = text if text == "" or text.endswith("\n") else text + "\n"
        yield Finding(ignore, f"add {', '.join(missing)}, so git never stores those files",
                      write(ignore, joined + "".join(f"{line}\n" for line in missing)))


CHECKS = (retired_runtime, azure_environment, dockerfile_exit_line, project_files)


def findings() -> list[Finding]:
    project = config.find_project()
    return [finding for check in CHECKS for finding in check(project)]


def run(assume_yes: bool) -> int:
    project = config.find_project()
    found = findings()
    changes = [finding for finding in found if finding.apply is not None]
    steps = [finding for finding in found if finding.apply is None]

    def where(finding: Finding) -> str:
        return finding.path.relative_to(project).as_posix()

    if changes:
        if not deploy.confirm([f"{where(f)}: {f.what}" for f in changes], assume_yes):
            return 1
        for finding in changes:
            finding.apply()
        console.done(f"Updated {len({f.path for f in changes})} file(s).")
    problems = config.validate()
    if steps or problems:
        console.say()
        console.heading("Still to do by hand:")
        for finding in steps:
            console.bullet(f"{where(finding)}: {finding.what}")
        for problem in problems:
            console.bullet(problem)
        return 1
    if not changes:
        console.done(f"This project is up to date for pdt {__version__}.")
    return 0
