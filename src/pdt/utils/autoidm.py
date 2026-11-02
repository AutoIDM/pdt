"""The AutoIDM registrations of this computer, and signed requests to the AutoIDM backend.

A registration is one customer and one installation. `pdt register` writes
each registration of this computer to `folder()`: <customer_id>.json (the
installation ID, the customer ID, and the API URL) and <customer_id>.key (the
Ed25519 private key, PEM). A cloud job has no such folder, so it reads
PDT_INSTALLATION_ID, PDT_INSTALLATION_KEY (the PEM text), PDT_API_URL, and
PDT_CUSTOMER_ID from its environment, or from the PDT_ENV_JSON secret that
holds them; when all four are set they win over the files, and a cloud
registration for another customer is an error, so a job never sends one
customer's runs under another customer's registration. `post` signs each request with a JWT
whose `body_sha256` claim holds the SHA-256 of the exact body bytes.
"""

from __future__ import annotations

import hashlib
import http.client
import json
import os
import secrets
import urllib.error
import urllib.parse
import urllib.request
import uuid
from base64 import urlsafe_b64encode
from dataclasses import dataclass
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
AUDIENCE = "pdt-backend"
TOKEN_SECONDS = 300
TIMEOUT_SECONDS = 10
UPLOAD_TIMEOUT_SECONDS = 300


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


def folder() -> Path:
    override = os.environ.get(DIR_ENV, "").strip()
    base = (Path(override) if override else
            config.machine_data_home() if os.name == "nt" else config.data_home() / "pdt")
    return base / "registrations"


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
    except ValueError:
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
    return Registration(saved["installation_id"], saved["api_url"].rstrip("/"), load_key(pem),
                        str(info))


def token(found: Registration, data: bytes) -> str:
    now = int(time())
    return jwt.encode({"iss": found.installation_id, "aud": AUDIENCE, "iat": now,
                       "exp": now + TOKEN_SECONDS, "jti": secrets.token_hex(16),
                       "body_sha256": hashlib.sha256(data).hexdigest()},
                      found.key, algorithm="EdDSA")


def send(url: str, data: bytes, headers: dict[str, str],
         timeout: int = TIMEOUT_SECONDS) -> dict:
    try:
        request = urllib.request.Request(url, data=data, method="POST",
                                         headers={"Content-Type": "application/json", **headers})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
    except urllib.error.HTTPError as e:
        try:
            answer = json.loads(e.read())
        except (ValueError, OSError, http.client.HTTPException):
            answer = {}
        answer = answer if isinstance(answer, dict) else {}
        code = str(answer.get("error") or "")
        raise AutoidmError(f"AutoIDM answered HTTP {e.code}: "
                           f"{answer.get('message') or code or e.reason}", e.code, code) from None
    except (urllib.error.URLError, OSError, http.client.HTTPException, ValueError) as e:
        raise AutoidmError(f"AutoIDM could not be reached at {url}: "
                           f"{getattr(e, 'reason', e)}") from None
    if not raw.strip().startswith(b"{"):
        return {}
    try:
        return json.loads(raw)
    except ValueError:
        raise AutoidmError(f"AutoIDM answered {url} with JSON that does not parse") from None


def answered(answer: dict, what: str, *names: str) -> dict:
    """`answer`, when each of `names` is a non-empty string in it."""
    missing = [name for name in names if not (isinstance(answer.get(name), str) and answer[name])]
    if missing:
        raise AutoidmError(f"AutoIDM answered {what} without {', '.join(missing)}")
    return answer


def register(api_url: str, body: dict) -> dict:
    from pdt import managed_services
    answer = answered(send(f"{api_url.rstrip('/')}/v1/register", json.dumps(body).encode(), {}),
                      "/v1/register", "installation_id", "customer_id")
    if not managed_services.CUSTOMER.fullmatch(answer["customer_id"]):
        raise AutoidmError(f"AutoIDM answered /v1/register with the customer ID "
                           f"{answer['customer_id']!r}, which is not one")
    return answer


def post(found: Registration, path: str, body: dict) -> dict:
    data = json.dumps(body).encode()
    return send(found.api_url + path, data, {"Authorization": f"Bearer {token(found, data)}"})


def upload(found: Registration, body: dict, data: bytes) -> str:
    """Ask /v1/uploads for a place for one file (`body` has its kind, project, app, run_id,
    and filename), send `data` there, and return its key."""
    place = answered(post(found, "/v1/uploads", {"version": 1, **body, "size": len(data)}),
                     "/v1/uploads", "url", "key")
    if not isinstance(place.get("fields"), dict):
        raise AutoidmError("AutoIDM answered /v1/uploads without fields")
    target = urllib.parse.urlsplit(place["url"])
    if not (target.scheme == "https" or (target.scheme == "http"
                                         and target.hostname in ("127.0.0.1", "localhost"))):
        raise AutoidmError(f"AutoIDM answered /v1/uploads with the URL {place['url']!r}, which "
                           "is not https")
    boundary = uuid.uuid4().hex
    parts = [f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'
             .encode() for name, value in place["fields"].items()]
    parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="file"; '
                 f'filename="{body["filename"]}"\r\n'
                 'Content-Type: application/octet-stream\r\n\r\n'.encode() + data + b"\r\n")
    send(place["url"], b"".join(parts) + f"--{boundary}--\r\n".encode(),
         {"Content-Type": f"multipart/form-data; boundary={boundary}"}, UPLOAD_TIMEOUT_SECONDS)
    return place["key"]
