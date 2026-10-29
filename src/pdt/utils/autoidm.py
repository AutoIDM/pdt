"""The AutoIDM installation of this computer, and signed requests to the AutoIDM backend.

`pdt register` writes the installation to `folder()`: installation.json (the
installation ID and the API URL) and installation.key (the Ed25519 private key,
PEM). A cloud job has no such folder, so it reads PDT_INSTALLATION_ID,
PDT_INSTALLATION_KEY (the PEM text), and PDT_API_URL instead; when all three are
set they win over the files. `post` signs each request with a JWT whose
`body_sha256` claim holds the SHA-256 of the exact body bytes, so an app's run.py
can call `autoidm.post("/v1/events", body)` as well.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from time import time

import jwt
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import load_pem_private_key

from pdt import config

DIR_ENV = "PDT_INSTALLATION_DIR"
ID_ENV = "PDT_INSTALLATION_ID"
KEY_ENV = "PDT_INSTALLATION_KEY"
URL_ENV = "PDT_API_URL"
INFO_FILE = "installation.json"
KEY_FILE = "installation.key"
AUDIENCE = "pdt-backend"
TOKEN_SECONDS = 300
TIMEOUT_SECONDS = 10


class AutoidmError(RuntimeError):
    """A request AutoIDM refused or never answered. `status` is 0 when it never answered,
    and `code` is the `error` of the answer."""

    def __init__(self, message: str, status: int = 0, code: str = ""):
        super().__init__(message)
        self.status = status
        self.code = code


@dataclass(frozen=True)
class Installation:
    installation_id: str
    api_url: str
    key: Ed25519PrivateKey


def folder() -> Path:
    override = os.environ.get(DIR_ENV, "").strip()
    if override:
        return Path(override)
    return config.machine_data_home() if os.name == "nt" else config.data_home() / "pdt"


def load_key(pem: bytes) -> Ed25519PrivateKey:
    key = load_pem_private_key(pem, password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise AutoidmError("the AutoIDM installation key is not an Ed25519 key")
    return key


def installation() -> Installation | None:
    """This computer's installation, from the environment or from `folder()`, or None."""
    env = [os.environ.get(name, "").strip() for name in (ID_ENV, KEY_ENV, URL_ENV)]
    if all(env):
        return Installation(env[0], env[2].rstrip("/"), load_key(env[1].encode()))
    info, key = folder() / INFO_FILE, folder() / KEY_FILE
    if not info.is_file():
        return None
    saved = json.loads(info.read_text(encoding="utf-8"))
    return Installation(saved["installation_id"], saved["api_url"].rstrip("/"),
                        load_key(key.read_bytes()))


def token(found: Installation, data: bytes) -> str:
    now = int(time())
    return jwt.encode({"iss": found.installation_id, "aud": AUDIENCE, "iat": now,
                       "exp": now + TOKEN_SECONDS, "jti": secrets.token_hex(16),
                       "body_sha256": hashlib.sha256(data).hexdigest()},
                      found.key, algorithm="EdDSA")


def send(url: str, data: bytes, headers: dict[str, str]) -> dict:
    request = urllib.request.Request(url, data=data, method="POST",
                                     headers={"Content-Type": "application/json", **headers})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            raw = response.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        try:
            answer = json.loads(e.read())
        except ValueError:
            answer = {}
        answer = answer if isinstance(answer, dict) else {}
        code = str(answer.get("error") or "")
        raise AutoidmError(f"AutoIDM answered HTTP {e.code}: "
                           f"{answer.get('message') or code or e.reason}", e.code, code) from None
    except urllib.error.URLError as e:
        raise AutoidmError(f"AutoIDM could not be reached at {url}: {e.reason}") from None


def register(api_url: str, body: dict) -> dict:
    """POST /v1/register, the one request with no token."""
    return send(f"{api_url.rstrip('/')}/v1/register", json.dumps(body).encode(), {})


def post(path: str, body: dict) -> dict:
    """POST `body` as JSON to `path`, such as /v1/events, signed by this installation."""
    found = installation()
    if found is None:
        raise AutoidmError("this computer is not registered to AutoIDM; "
                           "run pdt register --code CODE")
    data = json.dumps(body).encode()
    return send(found.api_url + path, data, {"Authorization": f"Bearer {token(found, data)}"})
