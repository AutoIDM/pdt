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

The image is built from DOCKERFILE unless the app directory holds its own
Dockerfile; write_dockerfile puts whichever applies at the root of the
context, so every cloud provider builds the same way. An app's own
.dockerignore is rewritten to the context root too (and as .gcloudignore,
which Cloud Build reads instead), so its patterns keep meaning paths
inside the app directory.

Every provider also shares one data store per account, named
`pdt-data-<suffix>` by `store_name` and tagged with `STORE_TAGS`; it
outlives any single app's deploy/destroy cycle.
"""

from __future__ import annotations

import base64
import dataclasses
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import urllib.error
import urllib.request
from http import HTTPStatus
from pathlib import Path

import backoff

from pdt import config, console

DOCKERFILE = """\
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim
COPY . /workspace
WORKDIR /workspace/{app}
ENV PDT_PROJECT=/workspace NO_COLOR=1
RUN uv sync --script run.py
ENTRYPOINT ["uv", "run", "--script", "run.py"]
"""
BUILD_EXCLUDES = (
    ".env", ".env.*", ".secrets", ".git", ".venv", "__pycache__",
    ".DS_Store", ".gcloud", "*.json.key", "*-key.json",
    "service-account*.json",
)
STORE_PREFIX = "pdt-data"
STORE_TAGS = {"managed-by": "pdt", "pdt-lifecycle": "retain"}


def store_suffix(seed: str) -> str:
    return hashlib.sha256(seed.encode()).hexdigest()[:10]


def store_name(seed: str) -> str:
    return f"{STORE_PREFIX}-{store_suffix(seed)}"


def own_dockerfile(app: dict) -> Path | None:
    """The app's own Dockerfile, when it ships one."""
    path = Path(app["dir"]) / "Dockerfile"
    return path if path.is_file() else None


def write_dockerfile(stage: Path, app: dict) -> None:
    """Put the Dockerfile to build at the root of the staged build context.

    An app's own Dockerfile is used as is. Any other app gets DOCKERFILE.
    The context is the same either way: the app directory under its own
    name next to pdt.yml, so a custom file starts from the generated one.
    """
    own = own_dockerfile(app)
    if own is not None:
        shutil.copy(own, stage / "Dockerfile")
    else:
        (stage / "Dockerfile").write_text(DOCKERFILE.format(app=app["name"]))
    ignore = Path(app["dir"]) / ".dockerignore"
    if ignore.is_file():
        text = context_ignore_text(ignore.read_text(), app["name"])
        (stage / ".dockerignore").write_text(text)
        (stage / ".gcloudignore").write_text(text)


def context_ignore_text(text: str, app_name: str) -> str:
    """Move .dockerignore patterns written for the app directory to the context root."""
    lines = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            lines.append(raw)
            continue
        negated = line.startswith("!")
        pattern = (line[1:] if negated else line).lstrip("/")
        lines.append(("!" if negated else "") + f"{app_name}/{pattern}")
    return "\n".join(lines) + "\n"


def image_action(app: dict, what: str) -> str:
    """One plan line for the image build, naming a custom Dockerfile."""
    if own_dockerfile(app) is not None:
        return f"{what} (from {app['name']}/Dockerfile)"
    return what


def fail(message: str) -> None:
    console.error(message)
    raise SystemExit(1)


@dataclasses.dataclass
class CostEstimate:
    """What a deploy will cost per month, shown before the user agrees.

    `items` pairs a label with a dollar amount. `prices` says where the
    numbers come from, such as "us-east-1 list prices, before free tiers".
    `excludes` names what the estimate leaves out.
    """
    items: list[tuple[str, float]]
    prices: str
    excludes: str = ""

    def show(self) -> None:
        console.cost(self.items, self.prices, self.excludes)
def store_plan_lines(description: str, exists: bool, identity: str, app_name: str) -> list[str]:
    return [("use existing" if exists else "create") + f" {description} (kept after destroy)",
            f"grant {identity} write access to {app_name}/ in {description}"]


def store_kept_line(description: str, count: int, app_name: str) -> str:
    return f"kept: {description} ({count} objects under {app_name}/)"


def store_cost_label(count: int, size_bytes: int) -> str:
    return f"storage: {count} objects ({size_bytes / 1024 ** 3:.2f} GB)"


def warn_if_locked(store, app_name: str) -> None:
    held = store.held_lock()
    if held is not None:
        console.warn(f"run {held['run']} of {app_name} started at {held['started']} "
                     "still holds the state; destroying now loses that run's state")


# HTTP fetching follows the Meltano SDK's RESTStream pattern:
# validate_response splits responses into retriable (429 and every 5xx)
# and fatal (any other 4xx), and backoff retries the retriable ones plus
# connection errors and timeouts with an exponential wait.
EXTRA_RETRY_STATUSES = (HTTPStatus.TOO_MANY_REQUESTS,)
BACKOFF_MAX_TRIES = 5


class FatalAPIError(Exception):
    """The server rejected the request; sending it again cannot help."""

    def __init__(self, message: str, response=None):
        super().__init__(message)
        self.response = response


class RetriableAPIError(Exception):
    """The server is busy or failing; sending the request again can work."""

    def __init__(self, message: str, response=None):
        super().__init__(message)
        self.response = response


def response_error_message(response) -> str:
    error_type = ("Client" if HTTPStatus.BAD_REQUEST <= response.status
                  < HTTPStatus.INTERNAL_SERVER_ERROR else "Server")
    return (f"{response.status} {error_type} Error: {response.reason} "
            f"for url: {response.url}")


def validate_response(response) -> None:
    if (response.status in EXTRA_RETRY_STATUSES
            or response.status >= HTTPStatus.INTERNAL_SERVER_ERROR):
        raise RetriableAPIError(response_error_message(response), response)
    if HTTPStatus.BAD_REQUEST <= response.status < HTTPStatus.INTERNAL_SERVER_ERROR:
        raise FatalAPIError(response_error_message(response), response)


def backoff_handler(details) -> None:
    console.bullet(f"the server did not answer (try {details['tries']} of "
                   f"{BACKOFF_MAX_TRIES}); backing off {details['wait']:.1f}s...", indent=4)


@backoff.on_exception(
    backoff.expo,
    (RetriableAPIError, ConnectionError, TimeoutError, urllib.error.URLError),
    max_tries=BACKOFF_MAX_TRIES,
    factor=2,
    on_backoff=backoff_handler,
)
def fetch_json(request: str | urllib.request.Request, timeout: int = 60):
    """GET a JSON document, backing off and retrying whatever can pass."""
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        validate_response(error)
        raise


def docker_preflight(provider: str) -> None:
    if not shutil.which("docker"):
        fail(f"Docker is required to deploy to {provider}; install Docker Desktop "
             "and run the same command again")
    proc = subprocess.run(["docker", "info"], capture_output=True, text=True, check=False)
    if proc.returncode:
        fail("Docker is installed but not running; start Docker and run the same command again")


def run_build(command: list[str]) -> None:
    proc = subprocess.run(
        command, capture_output=True, text=True, check=False)
    if proc.returncode:
        if proc.stdout.strip():
            console.say(proc.stdout.strip())
        if proc.stderr.strip():
            console.say(proc.stderr.strip())
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
