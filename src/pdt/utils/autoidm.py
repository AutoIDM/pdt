"""The AutoIDM registrations of this computer, and signed requests to the AutoIDM backend.

A registration is one customer and one installation. `pdt register` writes
each registration of this computer to `folder()`: <customer_id>.json (the
installation ID, the customer ID, the API URL, and the command keys) and
<customer_id>.key (the Ed25519 private key, PEM). `data_folder` is the folder
that holds them: %ProgramData%\\pdt on Windows, /var/lib/pdt for root on Linux,
/Library/Application Support/pdt for root on macOS, and the user's own data
folder for another user. A cloud job and a CI job have no such folder, so each reads
PDT_INSTALLATION_ID, PDT_INSTALLATION_KEY (the PEM text), PDT_API_URL, and
PDT_CUSTOMER_ID from its environment, or from the PDT_ENV_JSON secret that
holds them; when all four are set they win over the files, and a cloud
registration for another customer is an error, so a job never sends one
customer's runs under another customer's registration. `post` signs each request with a JWT
whose `body_sha256` claim holds the SHA-256 of the exact body bytes, and it sends a request again
after a 429 or a 503, with a growing wait. `clock_offset` corrects each token for a computer with
a wrong clock: the pdt service sets it at each check-in, and a 401 sets it from the answer's date.
"""

from __future__ import annotations

import hashlib
import http.client
import json
import os
import random
import secrets
import sys
import time as time_module
import urllib.error
import urllib.parse
import urllib.request
import uuid
from base64 import urlsafe_b64encode
from collections.abc import Iterator
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from pathlib import Path
from time import time

import jwt
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import (
    Encoding, NoEncryption, PrivateFormat, PublicFormat, load_pem_private_key)

from pdt import config

DIR_ENV = "PDT_INSTALLATION_DIR"
ID_ENV = "PDT_INSTALLATION_ID"
KEY_ENV = "PDT_INSTALLATION_KEY"
URL_ENV = "PDT_API_URL"
CUSTOMER_ENV = "PDT_CUSTOMER_ID"
SECRET_NAMES = (ID_ENV, KEY_ENV, URL_ENV, CUSTOMER_ENV)
# The hosted runner gives a deploy its command ID and run token, to make the cloud registration.
RUNNER_ENV = "PDT_RUNNER_REGISTRATION"
AUDIENCE = "pdt-backend"
WEBSOCKET_AUDIENCE = "pdt-ws"
TOKEN_SECONDS = 300
TIMEOUT_SECONDS = 10
UPLOAD_TIMEOUT_SECONDS = 15
UPLOAD_CHUNK_BYTES = 1024 * 1024
# The answers of API Gateway when a route passes its throttle or the function has no free slot.
RETRY_STATUSES = (429, 503)
RETRY_FIRST_SECONDS = 1.0
RETRY_MOST_SECONDS = 300.0
RETRY_LIMIT_SECONDS = 600.0
CLOCK_SKEW_SECONDS = 60
clock_offset = 0.0


class AutoidmError(RuntimeError):
    """A request AutoIDM refused or never answered. `status` is 0 when it never answered,
    and `code` is the `error` of the answer."""

    def __init__(self, message: str, status: int = 0, code: str = ""):
        super().__init__(message)
        self.status = status
        self.code = code


@dataclass(frozen=True)
class Registration:
    installation_id: str
    api_url: str
    key: Ed25519PrivateKey
    source: str


def data_folder() -> Path:
    override = os.environ.get(DIR_ENV, "").strip()
    if override:
        return Path(override)
    if os.name == "nt":
        return config.machine_data_home()
    if os.geteuid() == 0:
        return (Path("/Library/Application Support/pdt") if sys.platform == "darwin"
                else Path("/var/lib/pdt"))
    return config.data_home() / "pdt"


def folder() -> Path:
    return data_folder() / "registrations"


def files(customer: str) -> tuple[Path, Path]:
    return folder() / f"{customer}.json", folder() / f"{customer}.key"


def load_key(pem: bytes) -> Ed25519PrivateKey:
    key = load_pem_private_key(pem, password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise AutoidmError("the AutoIDM installation key is not an Ed25519 key")
    return key


def public_key(key: Ed25519PrivateKey) -> str:
    """The base64url of the 32 raw public key bytes, with no padding."""
    raw = key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    return urlsafe_b64encode(raw).decode().rstrip("=")


def private_pem(key: Ed25519PrivateKey) -> bytes:
    return key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption())


def environment_values() -> dict[str, str]:
    """The SECRET_NAMES from the environment, else from PDT_ENV_JSON, or {} when
    either place lacks one of them."""
    values = {name: os.environ.get(name, "").strip() for name in SECRET_NAMES}
    if all(values.values()):
        return values
    try:
        blob = json.loads(os.environ.get("PDT_ENV_JSON") or "{}")
    except (ValueError, RecursionError):
        return {}
    if not isinstance(blob, dict):
        return {}
    values = {name: str(blob.get(name) or "").strip() for name in SECRET_NAMES}
    return values if all(values.values()) else {}


def registration(customer: str) -> Registration | None:
    """The cloud registration in the environment, else this computer's registration for
    `customer`, else None."""
    found = environment_values()
    if found:
        if found[CUSTOMER_ENV] != customer:
            raise AutoidmError(f"the cloud registration {found[ID_ENV]} in the app's secret is "
                               f"for {found[CUSTOMER_ENV]}, and pdt.yml names {customer}; "
                               "deploy the app again to replace it")
        return Registration(found[ID_ENV], found[URL_ENV].rstrip("/"),
                            load_key(found[KEY_ENV].encode()), ID_ENV)
    return machine_registration(customer)


def machine_registration(customer: str) -> Registration | None:
    info, key = files(customer)
    if not info.is_file():
        return None
    try:
        saved = json.loads(info.read_text(encoding="utf-8"))
        pem = key.read_bytes()
    except OSError as e:
        raise AutoidmError(f"pdt cannot read {e.filename}: {e.strerror}") from None
    except (ValueError, RecursionError):
        raise AutoidmError(f"{info} does not hold JSON") from None
    if not (isinstance(saved, dict) and all(isinstance(saved.get(name), str) and saved[name]
                                            for name in ("installation_id", "api_url"))):
        raise AutoidmError(f"{info} does not name an installation_id and an api_url as text")
    return Registration(saved["installation_id"], saved["api_url"].rstrip("/"), load_key(pem),
                        str(info))


def token(found: Registration, data: bytes) -> str:
    now = int(time() + clock_offset)
    return jwt.encode({"iss": found.installation_id, "aud": AUDIENCE, "iat": now,
                       "exp": now + TOKEN_SECONDS, "jti": secrets.token_hex(16),
                       "body_sha256": hashlib.sha256(data).hexdigest()},
                      found.key, algorithm="EdDSA")


def websocket_token(found: Registration) -> str:
    now = int(time() + clock_offset)
    return jwt.encode({"iss": found.installation_id, "aud": WEBSOCKET_AUDIENCE, "iat": now,
                       "exp": now + TOKEN_SECONDS, "jti": secrets.token_hex(16)},
                      found.key, algorithm="EdDSA")


def correct_clock(date: str | None) -> None:
    """A 401 can mean a wrong clock, and with a wrong clock no check-in can bring the time. The
    answer's date corrects the next token."""
    global clock_offset
    try:
        difference = parsedate_to_datetime(date).timestamp() - time()
    except (TypeError, ValueError):
        return
    if abs(difference - clock_offset) > CLOCK_SKEW_SECONDS:
        clock_offset = difference


def send(url: str, data: bytes | Iterator[bytes], headers: dict[str, str],
         timeout: int = TIMEOUT_SECONDS) -> dict:
    try:
        request = urllib.request.Request(url, data=data, method="POST",
                                         headers={"Content-Type": "application/json", **headers})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
    except urllib.error.HTTPError as e:
        try:
            answer = json.loads(e.read())
        except (ValueError, RecursionError, OSError, http.client.HTTPException):
            answer = {}
        answer = answer if isinstance(answer, dict) else {}
        code = str(answer.get("error") or "")
        if e.code == 401:
            correct_clock(e.headers.get("Date"))
        raise AutoidmError(f"AutoIDM answered HTTP {e.code}: "
                           f"{answer.get('message') or code or e.reason}", e.code, code) from None
    except (urllib.error.URLError, OSError, http.client.HTTPException, ValueError) as e:
        raise AutoidmError(f"AutoIDM could not be reached at {url}: "
                           f"{getattr(e, 'reason', e)}") from None
    if not raw.strip().startswith(b"{"):
        return {}
    try:
        return json.loads(raw)
    except (ValueError, RecursionError):
        raise AutoidmError(f"AutoIDM answered {url} with JSON that does not parse") from None


def answered(answer: dict, what: str, *names: str) -> dict:
    """`answer`, when each of `names` is a non-empty string in it."""
    missing = [name for name in names if not (isinstance(answer.get(name), str) and answer[name])]
    if missing:
        raise AutoidmError(f"AutoIDM answered {what} without {', '.join(missing)}")
    return answer


def register(api_url: str, body: dict) -> dict:
    from pdt import managed_services
    data = json.dumps(body).encode()
    answer = answered(retried(lambda: send(f"{api_url.rstrip('/')}/v1/register", data, {})),
                      "/v1/register", "installation_id", "customer_id")
    if not managed_services.CUSTOMER.fullmatch(answer["customer_id"]):
        raise AutoidmError(f"AutoIDM answered /v1/register with the customer ID "
                           f"{answer['customer_id']!r}, which is not one")
    return answer


def runner_installation(runner: dict, public_key_text: str) -> tuple[dict, str]:
    """The cloud registration that a hosted runner deploy makes with the command's run token,
    and the API URL it uses. `runner` holds api_url, command_id, and run_token."""
    api_url = runner["api_url"].rstrip("/")
    data = json.dumps({"version": 1, "command_id": runner["command_id"],
                       "public_key": public_key_text}).encode()
    answer = answered(retried(lambda: send(f"{api_url}/v1/runner/installations", data,
                                           {"Authorization": f"Bearer {runner['run_token']}"})),
                      "/v1/runner/installations", "installation_id", "customer_id")
    return answer, api_url


def retried(call, retry: bool = True) -> dict:
    """`call`, again after a 429 or a 503, with a growing random wait."""
    deadline = time_module.monotonic() + RETRY_LIMIT_SECONDS
    wait = RETRY_FIRST_SECONDS
    while True:
        try:
            return call()
        except AutoidmError as e:
            if not retry or e.status not in RETRY_STATUSES or time_module.monotonic() > deadline:
                raise
        time_module.sleep(random.uniform(wait / 2, wait))
        wait = min(wait * 2, RETRY_MOST_SECONDS)


def post(found: Registration, path: str, body: dict, retry: bool = True) -> dict:
    """A signed POST. A new try needs a new token, because a token holds a single-use `jti`."""
    data = json.dumps(body).encode()
    return retried(lambda: send(found.api_url + path, data,
                                {"Authorization": f"Bearer {token(found, data)}"}), retry)


def check_url(url: object, what: str) -> str:
    """`url` when it is https, or http to this computer (a test or a local server)."""
    try:
        target = urllib.parse.urlsplit(str(url))
        local = target.hostname in ("127.0.0.1", "localhost")
    except ValueError:
        raise AutoidmError(f"AutoIDM answered {what} with the URL {url!r}, which "
                           "does not parse") from None
    if not (target.scheme == "https" or (target.scheme == "http" and local)):
        raise AutoidmError(f"AutoIDM answered {what} with the URL {url!r}, which "
                           "is not https")
    return str(url)


def file_chunks(head: bytes, path: Path, tail: bytes) -> Iterator[bytes]:
    yield head
    with path.open("rb") as file:
        while chunk := file.read(UPLOAD_CHUNK_BYTES):
            yield chunk
    yield tail


def post_file(place: dict, filename: str, data: bytes | Path, what: str) -> None:
    """Send `data` to a presigned POST: `place` holds its url and its fields. A Path goes up in
    chunks, so a large file is not held in memory."""
    fields = place.get("fields")
    if not (isinstance(fields, dict)
            and all(isinstance(value, str) for item in fields.items() for value in item)):
        raise AutoidmError(f"AutoIDM answered {what} without fields of text")
    url = check_url(place["url"], what)
    boundary = uuid.uuid4().hex
    try:
        parts = [f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n'
                 f'{value}\r\n'.encode() for name, value in fields.items()]
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="file"; '
                     f'filename="{filename}"\r\n'
                     'Content-Type: application/octet-stream\r\n\r\n'.encode())
    except UnicodeEncodeError as e:
        raise AutoidmError(f"AutoIDM answered {what} with fields pdt cannot send: {e}") from None
    head = b"".join(parts)
    tail = f"\r\n--{boundary}--\r\n".encode()
    headers = {"Content-Type": f"multipart/form-data; boundary={boundary}"}
    if isinstance(data, Path):
        headers["Content-Length"] = str(len(head) + data.stat().st_size + len(tail))
        send(url, file_chunks(head, data, tail), headers, UPLOAD_TIMEOUT_SECONDS)
    else:
        send(url, head + data + tail, headers, UPLOAD_TIMEOUT_SECONDS)


def upload(found: Registration, body: dict, data: bytes | Path) -> str:
    """Ask /v1/uploads for a place for one file (`body` has its kind, project, app, run_id,
    and filename), send `data` there, and return its key."""
    size = data.stat().st_size if isinstance(data, Path) else len(data)
    place = answered(post(found, "/v1/uploads", {"version": 1, **body, "size": size}),
                     "/v1/uploads", "url", "key")
    post_file(place, body["filename"], data, "/v1/uploads")
    return place["key"]
