"""Entra ID app-only authentication shared by the apps in this repo."""

from __future__ import annotations

import os
import urllib.parse
from base64 import b64decode, b64encode
from collections.abc import Iterator
from pathlib import Path
from time import time
from uuid import uuid4

import jwt
from cryptography.hazmat.primitives.hashes import SHA1
from cryptography.hazmat.primitives.serialization.pkcs12 import (
    load_key_and_certificates,
)

from pdt.config import ConfigError, find_project
from pdt.utils.log import die, log
from pdt.utils.web import http_json

_EXIT_CONFIG = 1
GRAPH_SCOPE = "https://graph.microsoft.com/.default"
CERT_JWT_SECONDS = 60 * 10


def _env(name: str) -> str:
    return os.environ.get(name, "").strip()


def _load_pfx() -> bytes | None:
    b64 = _env("PDT_AZURE_PRIVATE_KEY_B64")
    if b64 != "":
        try:
            return b64decode(b64, validate=True)
        except ValueError:
            die(_EXIT_CONFIG, "PDT_AZURE_PRIVATE_KEY_B64 is not valid base64")
    raw = _env("PDT_AZURE_PRIVATE_KEY_PATH")
    if raw == "":
        return None
    path = Path(raw).expanduser()
    if not path.is_absolute():
        try:
            path = find_project() / path
        except ConfigError:
            path = Path.cwd() / path
    if not path.is_file():
        die(_EXIT_CONFIG, "certificate file not found", path=str(path))
    return path.read_bytes()


def _jwt_from_pfx(pfx: bytes, key_password: str, client_id: str, token_url: str) -> str:
    password = key_password.encode() if key_password != "" else None
    try:
        private_key, cert, _extra = load_key_and_certificates(pfx, password)
    except ValueError as e:
        die(_EXIT_CONFIG, "certificate could not be loaded (wrong or missing password?)",
            error=str(e))
    if private_key is None or cert is None:
        die(_EXIT_CONFIG, "certificate had no private key or cert")

    now = int(time())
    headers = {
        "alg": "RS256",
        "typ": "JWT",
        "x5t": b64encode(cert.fingerprint(SHA1())).decode("ascii"),
    }
    payload = {
        "aud": token_url,
        "exp": now + CERT_JWT_SECONDS,
        "iss": client_id,
        "jti": str(uuid4()),
        "nbf": now,
        "sub": client_id,
    }
    return jwt.encode(payload, private_key, algorithm="RS256", headers=headers)


def graph_token(exit_code: int) -> str:
    tenant_id = _env("PDT_AZURE_TENANT_ID")
    client_id = _env("PDT_AZURE_CLIENT_ID")
    url = f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"
    body = {"scope": GRAPH_SCOPE, "client_id": client_id, "grant_type": "client_credentials"}
    pfx = _load_pfx()
    if pfx is None:
        log("info", "using client secret auth")
        body["client_secret"] = _env("PDT_AZURE_CLIENT_SECRET")
    else:
        log("info", "using certificate auth")
        body["client_assertion_type"] = "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"
        body["client_assertion"] = _jwt_from_pfx(
            pfx, _env("PDT_AZURE_PRIVATE_KEY_PASSWORD"), client_id, url)
    payload = http_json(url, exit_code, method="POST",
                        headers={"Content-Type": "application/x-www-form-urlencoded"},
                        data=urllib.parse.urlencode(body).encode())
    token = payload.get("access_token", "")
    if not isinstance(token, str) or token == "":
        die(exit_code, "token response had no access_token")
    return token


def graph_pages(token: str, url: str, exit_code: int) -> Iterator[list]:
    """Yield each Graph page's value list, following @odata.nextLink."""
    while url:
        payload = http_json(url, exit_code, headers={"Authorization": f"Bearer {token}"})
        rows = payload.get("value")
        if not isinstance(rows, list):
            die(exit_code, "Graph response had no value list", url=url)
        next_link = payload.get("@odata.nextLink") or ""
        if not isinstance(next_link, str):
            die(exit_code, "Graph response had invalid next link")
        yield rows
        url = next_link
