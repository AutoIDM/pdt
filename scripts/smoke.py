"""Build the wheel, install it, and drive the commands a new user runs.

The unit tests import pdt directly, so they cannot catch a packaging
mistake. GitLab CI and GitHub Actions both call it, so keep it free of
shell syntax and working on Windows.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
WHEEL_MUST_HOLD = (
    "pdt/cli.py",
    "pdt/scaffold.py",
    "pdt/deploy_azure.py",
    "pdt/utils/send_email.py",
    "pdt/examples/impossible-travel-report/run.py",
    "pdt/examples/impossible-travel-report/config.yml",
    "pdt/examples/impossible-travel-report/env.template",
    "pdt/examples/hello-world/run.py",
    "pdt/examples/hello-world/config.yml",
)

failures = []


def check(ok: bool, label: str) -> None:
    print(f"{'PASS' if ok else 'FAIL'}  {label}")
    if not ok:
        failures.append(label)


def run(command: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(command, cwd=cwd, text=True, capture_output=True)


def uv(*args: str, cwd: Path = REPO) -> subprocess.CompletedProcess:
    return run(["uv", *args], cwd)


def console_script(venv: Path) -> Path:
    if os.name == "nt":
        return venv / "Scripts" / "pdt.exe"
    return venv / "bin" / "pdt"


def main() -> int:
    work = Path(tempfile.mkdtemp(prefix="pdt-smoke-"))
    try:
        dist = work / "dist"
        built = uv("build", "--wheel", "--out-dir", str(dist))
        check(built.returncode == 0, "wheel builds")
        if built.returncode != 0:
            print(built.stderr)
            return 1

        wheel = next(dist.glob("*.whl"))
        names = zipfile.ZipFile(wheel).namelist()
        missing = [item for item in WHEEL_MUST_HOLD if item not in names]
        check(not missing, f"wheel holds every shipped file (missing: {missing})")

        venv = work / "venv"
        uv("venv", str(venv))
        installed = uv("pip", "install", "--python", str(venv), str(wheel))
        check(installed.returncode == 0, "wheel installs")
        if installed.returncode != 0:
            print(installed.stderr)
            return 1

        pdt = console_script(venv)
        check(pdt.is_file(), "the pdt console script exists")

        version = run([str(pdt), "--version"], work)
        check(version.returncode == 0 and version.stdout.strip() != "",
              f"pdt --version prints a version ({version.stdout.strip()})")

        examples = run([str(pdt), "examples"], work)
        check("impossible-travel-report" in examples.stdout,
              "pdt examples lists the bundled examples")

        outside = run([str(pdt), "list"], work)
        check("pdt init" in outside.stdout + outside.stderr,
              "pdt outside a project points at pdt init")

        created = run([str(pdt), "init", "reports", "--yes"], work)
        project = work / "reports"
        check(created.returncode == 0 and (project / "pdt.yml").is_file(),
              "pdt init creates a project")

        starter = project / "hello-world"
        check(starter.is_file() is False and (starter / "run.py").is_file(),
              "init put a starter app in the new project")
        check("PDT_VERSION" not in (starter / "run.py").read_text(),
              "the starter app pins the installed version")

        busy = work / "busy"
        busy.mkdir()
        (busy / "notes.txt").write_text("mine\n")
        run([str(pdt), "init", "--yes"], busy)
        check(not (busy / "hello-world").exists(),
              "init adds no starter to a folder that already holds files")

        added = run([str(pdt), "new", "my-report", "--from",
                     "impossible-travel-report"], project)
        check(added.returncode == 0 and (project / "my-report" / "run.py").is_file(),
              "pdt new copies a bundled example")

        pinned = (project / "my-report" / "run.py").read_text()
        check("PDT_VERSION" not in pinned and "pdt-cli[apps]==" in pinned,
              "the copied run.py pins the installed version")

        guided = run([str(pdt), "new", "another"], project)
        check(guided.returncode != 0 and "impossible-travel-report" in guided.stdout,
              "pdt new without --from lists the choices")

        nested = project / "my-report"
        listed = run([str(pdt), "list"], nested)
        check("my-report" in listed.stdout,
              "pdt list walks up to the project from a nested folder")

        checked = run([str(pdt), "validate"], project)
        check("PDT_AZURE_TENANT_ID" in checked.stdout,
              "pdt validate reports the app's missing env vars")

        check(not (project / ".venv").exists() and not (project / "cli").exists(),
              "the tool leaves nothing behind in the project")
    finally:
        shutil.rmtree(work, ignore_errors=True)

    print()
    if failures:
        print(f"{len(failures)} check(s) failed")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
