"""Code shared by every provider deploy script.

Every env var the app declares goes into one json secret, mounted on the
job as PDT_ENV_JSON and expanded back into env vars by
pdt.config.load_env_json. A var that ends in _PATH is replaced by
<NAME>_B64 holding the base64 of the file it points to, because the
cloud job gets no files, only string secrets.

A build context holds the app directory and pdt.yml, nothing else. The
app's run.py declares pdt in its script header, so every deployment
installs the package from the index the same way a local run does.
PDT_PROJECT names the project directory, so no job depends on its cwd.
"""

from __future__ import annotations

import base64
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from pdt import config

DOCKERFILE = """\
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim
COPY . /workspace
WORKDIR /workspace/{app}
ENV PDT_PROJECT=/workspace
RUN uv sync --script run.py
ENTRYPOINT ["uv", "run", "--script", "run.py"]
"""
BUILD_EXCLUDES = (
    ".env", ".env.*", ".secrets", ".git", ".venv", "__pycache__",
    ".DS_Store", ".gcloud", "*.json.key", "*-key.json",
    "service-account*.json",
)


def fail(message: str) -> None:
    print(f"error: {message}")
    raise SystemExit(1)


def run_build(command: list[str]) -> None:
    proc = subprocess.run(
        command, capture_output=True, text=True, check=False)
    if proc.returncode:
        if proc.stdout.strip():
            print(proc.stdout.strip())
        if proc.stderr.strip():
            print(proc.stderr.strip())
        fail(f"{' '.join(command[:3])} failed")


def gather_secrets(app: dict) -> dict[str, str]:
    spec = app["env"]
    names = list(spec.get("required") or [])
    for group in spec.get("one_of") or []:
        names.extend(group)
    names.extend(spec.get("optional") or [])
    values = {}
    for name in names:
        value = os.environ.get(name, "").strip()
        if value and name not in values:
            values[name] = value
    for name in [key for key in values if key.endswith("_PATH")]:
        target = name[:-5] + "_B64"
        path = Path(values[name]).expanduser()
        if not path.is_absolute():
            bases = [env_file.parent for env_file in config.find_env_files(app["dir"])]
            bases += [app["dir"], config.find_project()]
            path = next((base / path for base in bases if (base / path).is_file()), path)
        if not path.is_file():
            fail(f"{name} points to {path}, which does not exist")
        values.setdefault(target, base64.b64encode(path.read_bytes()).decode("ascii"))
        del values[name]
    return values


def stage_build_context(app: dict) -> Path:
    # Stage a clean build context so .env and .secrets never reach the image.
    stage = Path(tempfile.mkdtemp(prefix="pdt-build-"))
    skip = shutil.ignore_patterns(*BUILD_EXCLUDES)
    try:
        shutil.copytree(app["dir"], stage / app["name"], ignore=skip)
        project_file = config.find_project() / config.PROJECT_FILE
        if project_file.is_file():
            shutil.copy(project_file, stage / config.PROJECT_FILE)
        return stage
    except BaseException:
        shutil.rmtree(stage, ignore_errors=True)
        raise
