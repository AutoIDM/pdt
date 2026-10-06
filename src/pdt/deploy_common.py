"""Code shared by every provider deploy script.

Every env var the app declares goes into one json secret, mounted on the
job as PDT_ENV_JSON and expanded back into env vars by
pdt.config.load_env_json. A var that ends in _PATH and names a local
file is replaced by <NAME>_B64 holding the file's base64, because the
cloud job gets no files, only string secrets. One that names no file is
a run-time location the app sets itself and goes through as is.

PDT_ENV_SECRET_RESOURCE names that secret. The job's identity may update
it and no other, through `pdt.utils.env_secret`.

A build context holds the app directory and pdt.yml, nothing else. The
app's run.py declares pdt in its script header, so every deployment
installs the package from the index the same way a local run does. An app
made of .ps1 files has no run.py; POWERSHELL_DOCKERFILE installs
pdt-cli[apps] at this pdt's version, then pwsh through that pdt's own
`pdt.pwsh` (the pinned, checksum-verified archive), then the PowerShell
modules its scripts need (each one exercised, so a broken module fails the
build), and runs `pdt.run_powershell`, or the app's own run.py when
`pdt new APP --from-scripts` wrote one.
PDT_PROJECT names the project directory, so no job depends on its cwd.

BUILD_EXCLUDES leaves secret shapes (env files, keys, certificates, credentials files, ssh and package-manager logins) and local state out of the context, and one note names what it left out. A symbolic link stays a link when its target is inside the app directory, and the build stops when one points outside, so no file from elsewhere on the machine reaches the image.

The image is built from DOCKERFILE (or POWERSHELL_DOCKERFILE) unless the
app directory holds its own Dockerfile; write_dockerfile puts whichever
applies at the root of the context, so every cloud provider builds the
same way. An app's own
.dockerignore is rewritten to the context root too (and as .gcloudignore,
which Cloud Build reads instead), so its patterns keep meaning paths
inside the app directory. The generated image ends every run's output
with the line `pdt: exit N`, which `pdt runs` reads for the run's status.

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
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from http import HTTPStatus
from pathlib import Path
from typing import Callable

import backoff

from pdt import config, console
from pdt.utils.email_auth import can_prompt
from pdt.utils.env_secret import private_file

DOCKERFILE = """\
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim
COPY . /workspace
WORKDIR /workspace/{app}
ENV PDT_PROJECT=/workspace NO_COLOR=1 DBT_USE_COLORS=false
RUN uv sync --script run.py
ENTRYPOINT ["sh", "-c", "uv run --script run.py; code=$?; echo \\"pdt: exit $code\\"; exit $code"]
"""
POWERSHELL_DOCKERFILE = """\
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates libicu72 libssl3 libgssapi-krb5-2 && rm -rf /var/lib/apt/lists/*
ENV POWERSHELL_TELEMETRY_OPTOUT=1 PDT_PROJECT=/workspace NO_COLOR=1
RUN uv venv /opt/pdt && uv pip install --python /opt/pdt "pdt-cli[apps]=={version}"
RUN ln -s "$(/opt/pdt/bin/python -m pdt.pwsh)" /usr/local/bin/pwsh
{modules}COPY . /workspace
WORKDIR /workspace/{app}
{sync}ENTRYPOINT ["sh", "-c", "{start}; code=$?; echo \\"pdt: exit $code\\"; exit $code"]
"""
# Secret shapes and local state that never belong in an image.
BUILD_EXCLUDES = (
    ".env", ".env.*", ".secrets", ".git", ".venv", "__pycache__",
    ".DS_Store", ".gcloud", "*.json.key", "*-key.json",
    "service-account*.json", "*.pem", "*.key", "*.p12", "*.pfx", "*.jks",
    "credentials.json", "credentials", "id_rsa*", "id_ed25519*", "id_ecdsa*",
    ".ssh", ".pgpass", ".netrc", ".npmrc", ".pypirc", ".pdt", ".meltano",
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


def powershell_dockerfile(app: dict) -> str:
    from pdt import __version__, powershell
    modules = powershell.scan(app, app["platform"]["provider"]).modules
    # Exec form, because the install command holds $ and quotes that sh would expand.
    command = json.dumps(["pwsh", "-NoProfile", "-Command", powershell.install_command(modules)])
    sync, start = "", "/opt/pdt/bin/python -m pdt.run_powershell ."
    if (Path(app["dir"]) / "run.py").is_file():
        sync, start = "RUN uv sync --script run.py\n", "uv run --script run.py"
    return POWERSHELL_DOCKERFILE.format(
        app=app["name"], version=__version__, modules=f"RUN {command}\n" if modules else "",
        sync=sync, start=start)


def write_dockerfile(stage: Path, app: dict) -> None:
    """Put the Dockerfile to build at the root of the staged build context.

    An app's own Dockerfile is used as is. An app made of .ps1 files gets
    POWERSHELL_DOCKERFILE, any other app DOCKERFILE.
    The context is the same either way: the app directory under its own
    name next to pdt.yml, so a custom file starts from the generated one.
    """
    own = own_dockerfile(app)
    if own is not None:
        shutil.copy(own, stage / "Dockerfile")
    elif config.powershell_scripts(app["dir"]):
        (stage / "Dockerfile").write_text(powershell_dockerfile(app))
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
            console.note(f"{name} is not a file here; the job gets it as a plain value")
            continue
        values.setdefault(target, base64.b64encode(path.read_bytes()).decode("ascii"))
        del values[name]
    problems = config.check_env(spec, values)
    if problems:
        for problem in problems:
            console.error(f"env: {problem}")
        fail("the secret bundle does not satisfy the app's env spec")
    return values


MASK_BOTH_ENDS_FROM = 12


def masked(value: str) -> str:
    """Enough of a secret to recognise it, never enough to use it."""
    value = str(value)
    if len(value) < MASK_BOTH_ENDS_FROM:
        return f"•••• ({len(value)} chars)"
    return f"{value[:2]}••••{value[-2:]} ({len(value)} chars)"


def secret_changes(current: str | None, values: dict[str, str]) -> list[tuple[str, str, str, str]]:
    """(kind, name, masked before, masked after) per env var, grouped by kind, then by name."""
    try:
        deployed = json.loads(current or "{}")
    except ValueError:
        deployed = {}
    changes = []
    for name in sorted(set(deployed) | set(values)):
        before = masked(deployed[name]) if name in deployed else ""
        after = masked(values[name]) if name in values else ""
        if not before:
            kind = "new"
        elif not after:
            kind = "deleted"
        elif deployed[name] != values[name]:
            kind = "updated"
        else:
            kind = "unchanged"
        changes.append((kind, name, before, after))
    kinds = list(console.SECRET_CHANGE_COLOURS)
    return sorted(changes, key=lambda change: (kinds.index(change[0]), change[1]))


SECRET_ACTIONS = ("diff", "save", "get", "set")


def run_secrets(action: str, app: dict, current: str | None,
                write: Callable[[dict[str, str]], None], assume_yes: bool,
                name: str | None = None) -> int:
    """`pdt secrets <app> diff|save|get|set`, once the provider has read the deployed secret."""
    from pdt.deploy import proceed
    if action == "set":
        return set_secret(app, current, write, name)
    if current is None:
        fail(f"{app['name']} has no deployed secrets yet. Run `pdt deploy {app['name']}` first.")
    if action == "get":
        return get_secrets(app, current, assume_yes)
    values = gather_secrets(app)
    if not values:
        console.note("this app declares no env vars, so it has no secrets.")
        return 0
    changes = secret_changes(current, values)
    console.secret_changes(changes)
    if all(kind == "unchanged" for kind, _name, _before, _after in changes):
        console.done("The deployed secrets already match your .env file.")
        return 0
    if action == "diff":
        console.say()
        console.command(f"pdt secrets {app['name']} save", "send these values to the deployed app")
        return 0
    if not proceed(assume_yes):
        console.warn("Aborted; nothing was changed.")
        return 0
    write(values)
    console.done(f"Updated the secrets of {app['name']}. The next run uses them.")
    return 0


def set_secret(app: dict, current: str | None, write: Callable[[dict[str, str]], None],
               name: str | None) -> int:
    """Put one value, read from stdin, into the deployed secret."""
    if not name:
        fail("pdt secrets <app> set needs the env var name, with the value on stdin.")
    if current is None:
        console.note(f"{app['name']} is not deployed, so there is no secret to update.")
        return 0
    value = sys.stdin.read().strip()
    if value == "":
        fail(f"no value for {name} on stdin.")
    try:
        values = json.loads(current)
    except ValueError:
        fail("the deployed secret is not the JSON that pdt writes, so pdt cannot update it.")
    if values.get(name) == value:
        console.done(f"{name} already has that value in the secrets of {app['name']}.")
        return 0
    values[name] = value
    write(values)
    console.done(f"Updated {name} in the secrets of {app['name']}. The next run uses it.")
    return 0


def get_secrets(app: dict, current: str, assume_yes: bool) -> int:
    """Copy the deployed secret into a .env file in the app folder."""
    from pdt.deploy import proceed
    try:
        values = json.loads(current)
    except ValueError:
        fail("the deployed secret is not the JSON that pdt writes, so pdt cannot read it.")
    default = f".env.{app['platform']['provider']}"
    answer = console.ask("Save to which file?", default) if can_prompt(None) else ""
    target = Path(app["dir"]) / (answer or default)
    if target.exists():
        console.warn(f"{target.name} already exists and will be replaced.")
        if not proceed(assume_yes):
            console.warn("Aborted; nothing was written.")
            return 0
    private_file(target)
    target.write_text("".join(env_line(name, values[name]) + "\n" for name in sorted(values)))
    console.done(f"Saved {len(values)} value(s) to {target}.")
    return 0


PLAIN_ENV_VALUE = re.compile(r"[A-Za-z0-9_./:@+=,%-]*")


def env_line(name: str, value: str) -> str:
    """One .env line that `dotenv` reads back to the same value."""
    value = str(value)
    if PLAIN_ENV_VALUE.fullmatch(value):
        return f"{name}={value}"
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    return f'{name}="{escaped}"'


def stage_build_context(app: dict) -> Path:
    app_dir = Path(app["dir"]).resolve()
    skip = shutil.ignore_patterns(*BUILD_EXCLUDES)
    for folder, dirs, files in os.walk(app_dir):
        left_out = skip(folder, dirs + files)
        dirs[:] = [name for name in dirs if name not in left_out]
        for name in {*dirs, *files} - left_out:
            path = Path(folder, name)
            if path.is_symlink() and not (target := path.resolve()).is_relative_to(app_dir):
                fail(f"{path.relative_to(app_dir)} is a link to {target}, "
                     "outside the app folder; the image must not carry it")
    stage = Path(tempfile.mkdtemp(prefix="pdt-build-"))
    try:
        shutil.copytree(app_dir, stage / app["name"], ignore=skip, symlinks=True)
        project_file = config.find_project() / config.PROJECT_FILE
        if project_file.is_file():
            shutil.copy(project_file, stage / config.PROJECT_FILE)
    except BaseException:
        shutil.rmtree(stage, ignore_errors=True)
        raise
    left_out = sorted(skip(app_dir, os.listdir(app_dir)))
    if left_out:
        console.note(f"left out of the image: {', '.join(left_out)}")
    return stage
