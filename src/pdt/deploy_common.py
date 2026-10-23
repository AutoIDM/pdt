"""Code shared by every provider deploy script.

Every env var the app declares goes into one json secret, mounted on the
job as PDT_ENV_JSON and expanded back into env vars by
pdt.config.load_env_json. A var that ends in _PATH and names a local
file is replaced by <NAME>_B64 holding the file's base64, because the
cloud job gets no files, only string secrets. One that names no file is
a run-time location the app sets itself and goes through as is.

PDT_ENV_SECRET_RESOURCE names that secret. The job's identity may update
it and no other, through `pdt.utils.env_secret`.

A build context holds the app directory, pdt.yml, and in .pdt-lifecycle
this pdt's lifecycle.py with the pdt modules it imports (LIFECYCLE_MODULES).
Every generated image copies that folder to /opt/pdt-lifecycle and starts
the app through it, so the job prints the lifecycle events of the pdt that
deployed it, as the Windows task does. The folder is not on the app's
sys.path, so it never shadows the pdt the app installs. The
app's run.py declares pdt in its script header, so every deployment
installs the package from the index the same way a local run does. An app
made of .ps1 files has no run.py; POWERSHELL_DOCKERFILE installs
pdt-cli[apps] at this pdt's version, then pwsh through that pdt's own
`pdt.pwsh` (the pinned, checksum-verified archive; its powershell.config.json
turns off the startup trace lines pwsh otherwise writes to the job's log), then the PowerShell
modules its scripts need (each one exercised, so a broken module fails the
build), and runs `pdt.run_powershell`, or the app's own run.py when
`pdt new APP --from-scripts` wrote one.
PDT_PROJECT names the project directory, so no job depends on its cwd.

BUILD_EXCLUDES leaves secret shapes (env files, keys, certificates, credentials files, ssh and package-manager logins) and local state out of the context, and one note names what it left out. A symbolic link stays a link when its target is inside the app directory, and the build stops when one points outside, so no file from elsewhere on the machine reaches the image.

The image is built from DOCKERFILE (or POWERSHELL_DOCKERFILE) unless the
app directory holds its own Dockerfile; write_dockerfile puts whichever
applies at the root of the context, so every cloud provider builds the
same way. An app's own
.dockerignore is rewritten to the context root too, so its patterns keep
meaning paths inside the app directory. The generated image ends every
run's output with the line `pdt: exit N`, which `pdt runs` reads for the
run's status.

Every cloud provider builds the image from the same staged context, the
same Dockerfile, the same pinned BASE_IMAGE, and the same target platform,
in one of two places, which `build_in_cloud` chooses before the deploy
touches the cloud. When `docker info` answers, Docker on this computer
builds the image and pushes it to the provider's registry. When it does
not, the provider's cloud builder (ACR Tasks, Cloud Build, CodeBuild)
builds it from an upload of the staged context. The `docker-build` key
(config.DOCKER_BUILDS) forces either place. A failed build stops the
deploy.

The job records `context_fingerprint` of its image's context under BUILD_TAG (`pdt-build`). `ensure_image` skips the build when the fingerprint is the same and the registry still holds the job's image, unless `pdt deploy --rebuild` sets REBUILD_ENV.

A local build forwards the deploying machine's SSH agent when one is
running, so a RUN step marked `--mount=type=ssh` can install a private
git dependency. A cloud build has no agent, so `build_in_cloud` stops a
deploy whose app needs one (`ssh_dependency`).

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
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ElementTree
from http import HTTPStatus
from pathlib import Path
from typing import Any, Callable

import backoff

from pdt import config, console, lifecycle
from pdt.utils.email_auth import can_prompt
from pdt.utils.env_secret import private_file

# The multi-platform index digest, so a local and a cloud build start from the same layers.
# To bump it, read the digest of the tag's index from ghcr.io.
BASE_IMAGE = ("ghcr.io/astral-sh/uv:python3.12-trixie-slim"
              "@sha256:9b8477a5f2e2f96fa99eaecb7680c1c4c9f527926430f3b630bfb2c3641f8f20")
# sh as PID 1 ignores SIGTERM, so the entrypoint passes a platform's stop on to
# lifecycle.py, which ends the run with job:run-failed, and the log still ends
# with `pdt: exit N`.
ENTRYPOINT = ('ENTRYPOINT ["sh", "-c", "stop() {{ kill -s $1 $pid; wait $pid; finish $?; }}; '
              'finish() {{ echo \\"pdt: exit $1\\"; exit $1; }}; '
              'trap \\"stop TERM\\" TERM; trap \\"stop INT\\" INT; '
              'uv run --script /opt/pdt-lifecycle/pdt/lifecycle.py -- {start} & pid=$!; '
              'wait $pid; finish $?"]\n')
DOCKERFILE = f"FROM {BASE_IMAGE}\n" + """\
COPY .pdt-lifecycle /opt/pdt-lifecycle
RUN uv sync --script /opt/pdt-lifecycle/pdt/lifecycle.py
WORKDIR /workspace/{app}
ENV PDT_PROJECT=/workspace NO_COLOR=1 DBT_USE_COLORS=false PYTHONUNBUFFERED=1
COPY .pdt-script .
RUN uv sync --script run.py
COPY . /workspace
""" + ENTRYPOINT.replace("{start}", "uv run --script run.py")
POWERSHELL_DOCKERFILE = f"FROM {BASE_IMAGE}\n" + """\
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates libicu76 libssl3t64 libgssapi-krb5-2 && rm -rf /var/lib/apt/lists/*
ENV POWERSHELL_TELEMETRY_OPTOUT=1 PDT_PROJECT=/workspace NO_COLOR=1 PYTHONUNBUFFERED=1
RUN uv venv /opt/pdt && uv pip install --python /opt/pdt "pdt-cli[apps]=={version}"
RUN pwsh="$(/opt/pdt/bin/python -m pdt.pwsh)" && ln -s "$pwsh" /usr/local/bin/pwsh && echo '{{"LogLevel":"Critical"}}' > "$(dirname "$pwsh")/powershell.config.json"
{modules}COPY .pdt-lifecycle /opt/pdt-lifecycle
RUN uv sync --script /opt/pdt-lifecycle/pdt/lifecycle.py
WORKDIR /workspace/{app}
{sync}COPY . /workspace
""" + ENTRYPOINT
# The PEP 723 script block, with the reference regex of the PEP.
SCRIPT_HEADER = re.compile(r"(?m)^# /// script$\s(?:^#(?:| .*)$\s)+^# ///$")
LIFECYCLE_MODULES = ("__init__.py", "alert_message.py", "config.py", "console.py", "lifecycle.py",
                     "slack.py", "teams.py", "utils/__init__.py", "utils/storage.py")
# Secret shapes and local state that never belong in an image. The version-control names are
# the ones `az acr build` always leaves out, so every build place gets the same context.
BUILD_EXCLUDES = (
    ".env", ".env.*", ".secrets", ".git", ".venv", "__pycache__",
    ".gitignore", ".bzr", "bzrignore", ".bzrignore", ".hg", ".hgignore", ".svn",
    ".DS_Store", ".gcloud", "*.json.key", "*-key.json",
    "service-account*.json", "*.pem", "*.key", "*.p12", "*.pfx", "*.jks",
    "credentials.json", "credentials", "id_rsa*", "id_ed25519*", "id_ecdsa*",
    ".ssh", ".pgpass", ".netrc", ".npmrc", ".pypirc", ".pdt", ".meltano",
)
BUILD_TAG = "pdt-build"
REBUILD_ENV = "PDT_REBUILD"
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
        sync, start = "COPY .pdt-script .\nRUN uv sync --script run.py\n", "uv run --script run.py"
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
    if own is None and (Path(app["dir"]) / "run.py").is_file():
        stage_script_header(stage, Path(app["dir"]))
    ignore = Path(app["dir"]) / ".dockerignore"
    if ignore.is_file():
        text = context_ignore_text(ignore.read_text(), app["name"])
        (stage / ".dockerignore").write_text(text)


def stage_script_header(stage: Path, app_dir: Path) -> None:
    """uv keeps a script's environment per script path, so the Dockerfile copies the
    folder to the app's own path, and `uv run --script run.py` finds that environment.
    """
    folder = stage / ".pdt-script"
    folder.mkdir()
    text = (app_dir / "run.py").read_text()
    header = SCRIPT_HEADER.search(text)
    (folder / "run.py").write_text(header.group(0) + "\n" if header else text)
    lock = app_dir / "run.py.lock"
    if lock.is_file():
        shutil.copy(lock, folder / lock.name)
    for wheel in app_dir.glob("*.whl"):
        shutil.copy(wheel, folder / wheel.name, follow_symlinks=False)


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


def image_action(app: dict, what: console.Markup, cloud_builder: str = "") -> console.Markup:
    """One plan line for the image build: where it builds, and a custom Dockerfile.

    `cloud_builder` names the provider's cloud builder when it builds the image, else "".
    """
    where = (f" in the cloud with {console.value(cloud_builder)}" if cloud_builder
             else " with Docker on this computer")
    if own_dockerfile(app) is not None:
        dockerfile = f"{app['name']}/Dockerfile"
        return console.Markup(f"{what}{where} (from {console.value(dockerfile)})")
    return console.Markup(f"{what}{where}")


def context_fingerprint(stage: Path, image_platform: str) -> str:
    """32 hex characters of a sha256 over pdt's version, the platform, and the staged context.

    Every file adds its path, size, and bytes, and every link its path and target, in sorted
    path order. Modes and mtimes do not count.
    """
    from pdt import __version__

    digest = hashlib.sha256()

    def add(*fields: str | bytes) -> None:
        for field in fields:
            data = field.encode() if isinstance(field, str) else field
            digest.update(f"{len(data)}:".encode() + data)

    add(__version__, image_platform)
    entries = []
    for folder, dirs, files in os.walk(stage, followlinks=False):
        for name in dirs + files:
            path = Path(folder, name)
            if path.is_symlink() or path.is_file():
                entries.append((path.relative_to(stage).as_posix(), path))
    for relative, path in sorted(entries):
        if path.is_symlink():
            add("link", relative, os.readlink(path))
        else:
            data = path.read_bytes()
            add("file", relative, str(len(data)), data)
    return digest.hexdigest()[:32]


def ensure_image(app: dict, image: str, image_platform: str, cloud_builder: str,
                 deployed: str | None, current: Callable[[], bool],
                 build: Callable[[Path], None]) -> str:
    """Build and push the image unless the job's fingerprint `deployed` is the same; return it.

    `current` says whether the registry still holds the image the job runs. It runs only
    when the fingerprints match. The staged context is removed on every path.
    """
    stage = stage_build_context(app)
    try:
        write_dockerfile(stage, app)
        fingerprint = context_fingerprint(stage, image_platform)
        if deployed == fingerprint and not os.environ.get(REBUILD_ENV):
            if current():
                console.done(f"Image {console.value(image)} is unchanged; "
                             "skipped the build and push.")
                return fingerprint
            console.note("the registry no longer holds the image the job runs, so the deploy "
                         "builds it again")
        where = (f"in the cloud with {console.value(cloud_builder)}" if cloud_builder
                 else "with Docker on this computer")
        console.step(f"building and pushing image {console.value(image)} {where}")
        build(stage)
    finally:
        shutil.rmtree(stage, ignore_errors=True)
    return fingerprint


def fail(message: str) -> None:
    console.error(message)
    raise SystemExit(1)


@dataclasses.dataclass
class CostEstimate:
    """What a deploy will cost per month, shown before the user agrees.

    `items` pairs a label with an amount in `currency`. `prices` says where
    the numbers come from, such as "us-east-1 list prices, before free tiers".
    `excludes` names what the estimate leaves out.
    """
    items: list[tuple[str, float]]
    prices: str
    excludes: str = ""
    currency: str = "USD"

    def show(self) -> None:
        console.cost(self.items, self.prices, self.excludes, self.currency)
        if self.currency != "USD":
            console.status(f"pdt shows {console.value(self.currency)}, the currency of "
                           "this computer's regional setting.")


PRICE_CACHE = config.data_home() / "pdt" / "prices"
PRICE_MAX_AGE_SECONDS = 72 * 3600


def cached_price(provider: str, region: str, currency: str, query: str,
                 read: Callable[[], Any], fetching: str = "") -> Any:
    key = {"provider": provider, "region": region, "currency": currency, "query": query}
    name = hashlib.sha256(json.dumps(key, sort_keys=True).encode()).hexdigest()
    path = PRICE_CACHE / f"{name}.json"
    entry = None
    try:
        saved = json.loads(path.read_text())
        if saved["key"] == key:
            entry = float(saved["read_at"]), saved["value"], saved["missing"]
    except (OSError, ValueError, LookupError, TypeError):
        pass
    if entry is None or not 0 <= time.time() - entry[0] < PRICE_MAX_AGE_SECONDS:
        if fetching:
            console.status_once(fetching)
        try:
            fresh = time.time(), read(), None
        except LookupError as exc:
            fresh = time.time(), None, str(exc)
        except Exception as exc:
            if entry is None:
                raise
            console.warn_once(f"Could not read current prices ({console.escape(str(exc))}); "
                              "the estimate uses saved prices.")
            fresh = None
        if fresh is not None:
            entry = fresh
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                config.write_text_atomically(path, json.dumps(
                    {"key": key, "read_at": entry[0], "value": entry[1], "missing": entry[2]}))
            except OSError:
                pass
    if entry[2] is not None:
        raise LookupError(entry[2])
    return entry[1]


ECB_RATES = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml"


def ecb_xml() -> bytes:
    # The ECB certificate chains to Sectigo Root E46, which the CA file of
    # uv's Python builds lacks. certifi holds it, and every provider script
    # installs certifi.
    try:
        import certifi
        context = ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        context = None
    with urllib.request.urlopen(ECB_RATES, timeout=30, context=context) as response:
        return response.read()


def ecb_rate(currency: str) -> tuple[float, str]:
    """What one USD costs in `currency` at the ECB euro reference rates, and their date."""
    def read() -> list:
        rates = {"EUR": 1.0}
        date = ""
        for cube in ElementTree.fromstring(ecb_xml()).iter():
            if cube.get("time"):
                date = cube.get("time", "")
            if cube.get("currency") and cube.get("rate"):
                rates[cube.get("currency", "")] = float(cube.get("rate", ""))
        if "USD" not in rates or currency not in rates:
            raise LookupError(f"the ECB publishes no {currency} rate")
        return [rates[currency] / rates["USD"], date]

    rate, date = cached_price("ecb", "", currency, ECB_RATES, read)
    return rate, date


def convert_from_usd(items: list[tuple[str, float]], currency: str,
                     published: tuple[str, Callable[[], float]] | None = None,
                     ) -> tuple[list[tuple[str, float]], str, str]:
    """Convert USD amounts to `currency`: the items, their currency, and a note for `prices`.

    `published` names an exchange rate the cloud itself publishes and the
    function that reads it. That rate comes first, then the ECB rate. When
    neither can be read, the items stay in USD and the note says why.
    """
    if currency == "USD":
        return items, "USD", ""
    if published is not None:
        name, read_rate = published
        try:
            rate = read_rate()
            return ([(label, amount * rate) for label, amount in items], currency,
                    f", converted from USD at {name}")
        except Exception:
            pass
    try:
        rate, date = ecb_rate(currency)
    except Exception as exc:
        return items, "USD", f" in USD, because no USD to {currency} rate could be read ({exc})"
    return ([(label, amount * rate) for label, amount in items], currency,
            f", converted from USD at the ECB rate of {date}")
def store_plan_lines(description: console.Markup, exists: bool, identity: str,
                     app_name: str) -> list[console.Markup]:
    return [console.Markup(("use existing" if exists else "create")
                           + f" {description} (kept after destroy)"),
            console.Markup(f"grant {console.value(identity)} write access to "
                           f"{console.value(f'{app_name}/')} in {description}")]


def store_kept_line(description: console.Markup, count: int, app_name: str) -> console.Markup:
    return console.Markup(f"kept: {description} ({count} objects under "
                          f"{console.value(f'{app_name}/')})")


def store_cost_label(count: int, size_bytes: int) -> str:
    return f"storage: {count} objects ({size_bytes / 1024 ** 3:.2f} GB)"


def warn_if_locked(store, app_name: str) -> None:
    held = store.held_lock()
    if held is not None:
        console.warn(f"run {console.value(held['run'])} of {console.value(app_name)} started at "
                     f"{console.escape(held['started'])} "
                     "still holds the state; destroying now loses that run's state. "
                     "If that run is no longer running, release the lock with: "
                     f"{console.value('pdt storage ' + app_name + ' unlock')}")


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


def fetch_json(request: str | urllib.request.Request, timeout: int = 60,
               open_url: Callable | None = None):
    host = urllib.parse.urlsplit(getattr(request, "full_url", request)).netloc
    with console.wait_on(console.value(host), "answered") as wait:
        return fetch_once(request, timeout, open_url, wait=wait)


@backoff.on_exception(
    backoff.expo,
    (RetriableAPIError, ConnectionError, TimeoutError, urllib.error.URLError),
    max_tries=BACKOFF_MAX_TRIES,
    factor=2,
    on_backoff=lambda details: details["kwargs"]["wait"].now(
        f"no answer (try {details['tries']} of {BACKOFF_MAX_TRIES})"),
)
def fetch_once(request: str | urllib.request.Request, timeout: int,
               open_url: Callable | None, wait: console.WaitOn):
    try:
        with (open_url or urllib.request.urlopen)(request, timeout=timeout) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        validate_response(error)
        raise


PIPES = "\\\\.\\pipe\\"
WINDOWS_AGENT = f"{PIPES}openssh-ssh-agent"


def ssh_build_args() -> list[str]:
    # docker build fails on a socket path that no longer exists, so a stale value is skipped.
    # Windows OpenSSH serves its agent on a named pipe, which docker uses when SSH_AUTH_SOCK is
    # unset. os.path.exists cannot see a pipe, so the pipe must be in the pipe listing.
    socket = os.environ.get("SSH_AUTH_SOCK", "").strip() or (WINDOWS_AGENT if os.name == "nt" else "")
    if os.name == "nt":
        socket = socket.replace("/", "\\")
    if os.name == "nt" and socket.startswith(PIPES):
        try:
            found = socket[len(PIPES):] in os.listdir(PIPES)
        except OSError:
            found = False
    else:
        found = bool(socket) and os.path.exists(socket)
    return ["--ssh", "default"] if found else []


DOCKER_INFO_SECONDS = 20
BINFMT_TAG = "qemu-v10.2.3"
BINFMT_DIGEST = "sha256:400a4873b838d1b89194d982c45e5fb3cda4593fbfd7e08a02e76b03b21166f0"
BINFMT = f"tonistiigi/binfmt:{BINFMT_TAG}@{BINFMT_DIGEST}"


def docker_problem() -> str:
    """"" when `docker info` answers, else why Docker on this computer cannot build."""
    if shutil.which("docker") is None:
        return "the docker command is not on the PATH"
    try:
        proc = subprocess.run(["docker", "info"], capture_output=True, text=True, check=False,
                              timeout=DOCKER_INFO_SECONDS)
    except subprocess.TimeoutExpired:
        return f"docker info did not answer within {DOCKER_INFO_SECONDS} seconds"
    if proc.returncode == 0:
        return ""
    lines = (proc.stderr or proc.stdout).strip().splitlines()
    return f"docker info failed: {lines[-1].strip() if lines else f'exit code {proc.returncode}'}"


def ssh_dependency(app: dict) -> str:
    """The SSH step in the app's own Dockerfile, which only a local build can forward, or ""."""
    own = own_dockerfile(app)
    if own is not None and "type=ssh" in own.read_text():
        return f"the RUN --mount=type=ssh step in {app['name']}/Dockerfile"
    return ""


def build_in_cloud(app: dict, builder: str, image_platform: str) -> bool:
    """Whether the provider's cloud `builder` builds the image, not Docker on this computer.

    Says which place builds and why. Stops the deploy when `docker-build: local` finds no
    Docker, and when a cloud build would need this computer's SSH agent.
    """
    setting, source = app["docker_build"], app["docker_build_from"]
    key = console.value(f"docker-build: {setting}")
    if setting == "cloud":
        console.status(f"{key} in {console.value(source)}, so the deploy will build "
                       f"the image in the cloud with {console.value(builder)}.")
    else:
        problem = console.escape(docker_problem())
        if problem == "" and config.unsaved() is not None:
            console.status(
                "Docker found. The deploy will build the image with Docker on this computer if "
                f"it can build {console.value(image_platform)} images, else "
                + (f"stop. {key} in {console.value(source)} allows only Docker."
                   if setting == "local" else f"in the cloud with {console.value(builder)}."))
            return False
        problem = problem or emulate(image_platform)
        if problem == "":
            console.status("Docker found, so the deploy will build the image with Docker on this "
                           "computer.")
            return False
        if setting == "local":
            fail(f"Docker on this computer cannot build the image: {problem}. "
                 f"{key} in {console.value(source)} builds only with Docker. Fix Docker and "
                 f"run the same command again, or remove {key} to build in the cloud.")
        console.status(f"Docker on this computer cannot build the image: {problem}. "
                       f"The deploy will build it in the cloud with {console.value(builder)}.")
    dependency = ssh_dependency(app)
    if dependency:
        fail(f"{console.value(app['name'])} needs {console.value(dependency)}, which installs "
             f"over SSH. A build in the cloud with {console.value(builder)} cannot use the SSH "
             "agent of this computer. A build with Docker on this computer can: start Docker"
             + (f", remove {key} from {console.value(source)}," if setting == "cloud" else "")
             + " and run the same command again.")
    return True


def emulate(image_platform: str) -> str:
    """"" when Docker on this computer can run `image_platform` programs, else why not.

    Docker runs another CPU type only through an emulator in the kernel that runs Docker.
    When the base image does not run, tonistiigi/binfmt at a pinned digest adds one.
    """
    if runs(image_platform):
        return ""
    cpu = image_platform.split("/")[1]
    command = ["docker", "run", "--privileged", "--rm", BINFMT, "--install", cpu]
    subprocess.run(command, capture_output=True, check=False)
    if runs(image_platform):
        console.status(f"Added the {console.value(cpu)} emulator with "
                       f"{console.value(' '.join(command))}, so Docker can build the "
                       f"{console.value(cpu)} image.")
        return ""
    return (f"it cannot run {console.value(image_platform)} programs, and "
            f"{console.value(' '.join(command))} did not add an emulator for them")


def runs(image_platform: str) -> bool:
    return subprocess.run(["docker", "run", "--rm", "--platform", image_platform, BASE_IMAGE,
                           "true"], capture_output=True, check=False).returncode == 0


def run_build(command: list[str], data: str | None = None, quiet: bool = False) -> None:
    """Run a docker command and show its output as it runs, or only on failure when `quiet`."""
    proc = subprocess.run(
        command, input=data, capture_output=quiet, text=True, check=False)
    if proc.returncode:
        if (proc.stdout or "").strip():
            console.say(console.escape(proc.stdout.strip()))
        if (proc.stderr or "").strip():
            console.say(console.escape(proc.stderr.strip()))
        fail(f"{console.value(' '.join(command[:3]))} failed")


def secret_contents(values: dict[str, str]) -> str:
    """The env var names a secret holds, for a plan line. A plan never shows a value."""
    return f"env vars {', '.join(map(console.value, sorted(values)))}" if values else "no env vars"


def gather_secrets(app: dict) -> dict[str, str]:
    try:
        spec = config.env_spec(app)
    except config.ConfigError as e:
        fail(console.escape(str(e)))
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
            console.note(f"{console.value(name)} is not a file here; the job gets it as a "
                         "plain value")
            continue
        values.setdefault(target, base64.b64encode(path.read_bytes()).decode("ascii"))
        del values[name]
    problems = config.check_env(spec, values)
    if problems:
        for problem in problems:
            console.error(f"env: {console.escape(problem)}")
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


def run_secrets(action: str, app: dict, current: str | None,
                write: Callable[[dict[str, str]], None], assume_yes: bool,
                name: str | None = None) -> int:
    """`pdt secrets <app> diff|save|get|set`, once the provider has read the deployed secret."""
    from pdt.deploy import proceed

    def update(values: dict[str, str]) -> None:
        count = sum(kind != "unchanged" for kind, *_rest in secret_changes(current, values))
        write(values)
        lifecycle.emit(lifecycle.APP_SECRETS_UPDATED,
                       **lifecycle.app_params(app["name"], app["platform"]["provider"]),
                       action=action, count=count)
        lifecycle.flush()

    if action == "set":
        return set_secret(app, current, update, name)
    if current is None:
        command = f"pdt deploy {app['name']}"
        fail(f"{console.value(app['name'])} has no deployed secrets yet. "
             f"Run `{console.value(command)}` first.")
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
    update(values)
    console.done(f"Updated the secrets of {console.value(app['name'])}. The next run uses them.")
    return 0


def set_secret(app: dict, current: str | None, write: Callable[[dict[str, str]], None],
               name: str | None) -> int:
    """Put one value, read from stdin, into the deployed secret."""
    if not name:
        fail(f"{console.value('pdt secrets <app> set')} needs the env var name, "
             "with the value on stdin.")
    if current is None:
        console.note(f"{console.value(app['name'])} is not deployed, so there is no secret "
                     "to update.")
        return 0
    value = sys.stdin.read().strip()
    if value == "":
        fail(f"no value for {console.value(name)} on stdin.")
    try:
        values = json.loads(current)
    except ValueError:
        fail("the deployed secret is not the JSON that pdt writes, so pdt cannot update it.")
    if values.get(name) == value:
        console.done(f"{console.value(name)} already has that value in the secrets of "
                     f"{console.value(app['name'])}.")
        return 0
    values[name] = value
    write(values)
    console.done(f"Updated {console.value(name)} in the secrets of {console.value(app['name'])}. "
                 "The next run uses it.")
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
        console.warn(f"{console.value(target.name)} already exists and will be replaced.")
        if not proceed(assume_yes):
            console.warn("Aborted; nothing was written.")
            return 0
    private_file(target)
    target.write_text("".join(env_line(name, values[name]) + "\n" for name in sorted(values)))
    console.done(f"Saved {len(values)} value(s) to {console.value(target)}.")
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
                fail(f"{console.value(path.relative_to(app_dir))} is a link to "
                     f"{console.value(target)}, "
                     "outside the app folder; the image must not carry it")
    stage = Path(tempfile.mkdtemp(prefix="pdt-build-"))
    try:
        shutil.copytree(app_dir, stage / app["name"], ignore=skip, symlinks=True)
        project_file = config.find_project() / config.PROJECT_FILE
        if project_file.is_file():
            shutil.copy(project_file, stage / config.PROJECT_FILE)
        for module in LIFECYCLE_MODULES:
            target = stage / ".pdt-lifecycle" / "pdt" / module
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(Path(__file__).parent / module, target)
    except BaseException:
        shutil.rmtree(stage, ignore_errors=True)
        raise
    left_out = sorted(skip(app_dir, os.listdir(app_dir)))
    if left_out:
        console.note("left out of the image: "
                     f"{', '.join(console.value(name) for name in left_out)}")
    return stage
