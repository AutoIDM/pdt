#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["pdt-cli[apps]==PDT_VERSION"]
# ///
"""Copy BambooHR employee photos onto matching Entra ID accounts.

Matches BambooHR workEmail to Entra mail and userPrincipalName. Comparison
ignores capitalization and surrounding spaces. A unique match is required.
Source of truth is the BambooHR photo. Entra photos are not cleared.

Needs a BambooHR company API key and an Entra app registration with
User.Read.All and ProfilePhoto.ReadWrite.All (application) plus admin consent.

Env (creds only -- put these in .env at this folder or any parent):
  Always:
    PDT_BAMBOOHR_API_KEY
    PDT_AZURE_TENANT_ID, PDT_AZURE_CLIENT_ID
  One of:
    PDT_AZURE_CLIENT_SECRET
    PDT_AZURE_PRIVATE_KEY_B64
    PDT_AZURE_PRIVATE_KEY_PATH (+ optional PDT_AZURE_PRIVATE_KEY_PASSWORD)
  Certificate auth wins over client-secret auth, and B64 wins over PATH.

Exit codes: 0 ok, 1 bad config, 2 Entra/Graph failure, 4 BambooHR failure.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import sys
import urllib.parse
from collections import defaultdict
from pathlib import Path

from pdt.config import ConfigError, check_env, load_env, merged_app
from pdt.utils.entra import graph_pages, graph_token
from pdt.utils.log import die, log
from pdt.utils.web import http_bytes, http_json

EXIT_OK = 0
EXIT_CONFIG = 1
EXIT_ENTRA = 2
EXIT_BAMBOO = 4

GRAPH_USERS = "https://graph.microsoft.com/v1.0/users"
PHOTO_SIZES = ("original", "large", "medium", "small", "xs", "tiny")
GRAPH_PHOTO_MAX_BYTES = 4 * 1024 * 1024


def cfg_str(cfg: dict, key: str) -> str:
    raw = str(cfg.get(key, "") or "").strip()
    if raw == "":
        die(EXIT_CONFIG, "config.yml missing key", key=key)
    return raw


def cfg_bool(cfg: dict, key: str) -> bool:
    raw = cfg.get(key, False)
    if isinstance(raw, bool):
        return raw
    text = str(raw or "").strip().casefold()
    if text in ("1", "true", "yes"):
        return True
    if text in ("0", "false", "no", ""):
        return False
    die(EXIT_CONFIG, "config.yml value is not true or false", key=key, value=raw)


def cfg_photo_size(cfg: dict) -> str:
    size = cfg_str(cfg, "photo_size").casefold()
    if size not in PHOTO_SIZES:
        die(EXIT_CONFIG, "config.yml photo_size is not valid",
            value=size, allowed=",".join(PHOTO_SIZES))
    return size


def normalize_email(value: str | None) -> str:
    return str(value or "").strip().casefold()


def photo_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def is_photo_uploaded(value) -> bool:
    if value is True:
        return True
    return str(value or "").strip().casefold() in ("true", "yes", "1")


def is_active_employee(value) -> bool:
    return str(value or "").strip().casefold() == "active"


def bamboo_headers(api_key: str, accept: str) -> dict:
    token = base64.b64encode(f"{api_key}:x".encode()).decode("ascii")
    return {
        "Authorization": f"Basic {token}",
        "Accept": accept,
        "Content-Type": "application/json",
        "User-Agent": "pdt/1.0",
    }


def bamboo_base(subdomain: str) -> str:
    return f"https://api.bamboohr.com/api/gateway.php/{subdomain}/v1"


def fetch_bamboo_employees(subdomain: str, api_key: str) -> list[dict]:
    url = f"{bamboo_base(subdomain)}/reports/custom?format=JSON"
    payload = http_json(
        url,
        EXIT_BAMBOO,
        method="POST",
        headers=bamboo_headers(api_key, "application/json"),
        data=json.dumps({
            "name": "PDT photo sync",
            "fields": ["id", "displayName", "workEmail", "status", "isPhotoUploaded"],
        }).encode(),
    )
    rows = payload.get("employees")
    if not isinstance(rows, list):
        die(EXIT_BAMBOO, "BambooHR report had no employees list")
    return rows


def fetch_bamboo_photo(subdomain: str, api_key: str, employee_id: str,
                       photo_size: str) -> bytes | None:
    quoted = urllib.parse.quote(employee_id, safe="")
    url = f"{bamboo_base(subdomain)}/employees/{quoted}/photo/{photo_size}"
    status, body = http_bytes(
        url,
        EXIT_BAMBOO,
        headers=bamboo_headers(api_key, "image/*"),
        allow_statuses=(404,),
    )
    if status == 404 or body == b"":
        return None
    if body[:1] == b"{":
        try:
            envelope = json.loads(body)
        except json.JSONDecodeError:
            return body
        inner = envelope.get("fileBase64") if isinstance(envelope, dict) else None
        if isinstance(inner, str) and inner != "":
            return base64.b64decode(inner)
    return body


def fetch_entra_users(token: str) -> list[dict]:
    params = urllib.parse.urlencode({
        "$select": "id,displayName,mail,userPrincipalName,accountEnabled",
        "$top": "999",
    })
    users = []
    for rows in graph_pages(token, f"{GRAPH_USERS}?{params}", EXIT_ENTRA):
        users.extend(rows)
        log("info", "fetched Entra user page", total=len(users))
    return users


def entra_photo_url(user_id: str) -> str:
    quoted = urllib.parse.quote(user_id, safe="")
    return f"{GRAPH_USERS}/{quoted}/photo/$value"


def fetch_entra_photo(token: str, user_id: str) -> bytes | None:
    status, body = http_bytes(
        entra_photo_url(user_id),
        EXIT_ENTRA,
        headers={"Authorization": f"Bearer {token}"},
        allow_statuses=(404,),
    )
    if status == 404:
        return None
    if body == b"":
        return None
    return body


def put_entra_photo(token: str, user_id: str, photo: bytes) -> None:
    http_bytes(
        entra_photo_url(user_id),
        EXIT_ENTRA,
        method="PUT",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "image/jpeg",
        },
        data=photo,
    )


def index_entra_by_email(rows: list[dict]) -> dict[str, list[dict]]:
    index: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        keys = set()
        for field in ("mail", "userPrincipalName"):
            email = normalize_email(row.get(field))
            if email != "":
                keys.add(email)
        for email in keys:
            index[email].append(row)
    return index


def employees_with_photos(rows: list[dict]) -> list[dict]:
    employees = []
    for row in rows:
        employee_id = str(row.get("id") or "").strip()
        if employee_id == "":
            continue
        if not is_active_employee(row.get("status")):
            continue
        if not is_photo_uploaded(row.get("isPhotoUploaded")):
            continue
        raw_email = str(row.get("workEmail") or "").strip()
        email = normalize_email(raw_email)
        if email == "":
            log("warning", "active BambooHR employee has a photo but no workEmail; skipped",
                employee_id=employee_id)
            continue
        employees.append({
            "id": employee_id,
            "name": str(row.get("displayName") or "").strip(),
            "email": raw_email,
            "normalized_email": email,
        })
    return employees


def main() -> int:
    app_dir = Path(__file__).resolve().parent
    try:
        env_files = load_env(app_dir)
    except ConfigError as e:
        die(EXIT_CONFIG, "bad env", error=str(e))
    for path in env_files:
        log("info", "loaded env file", path=str(path))

    try:
        app = merged_app(app_dir.name)
    except ConfigError as e:
        die(EXIT_CONFIG, "config error", error=str(e))
    problems = check_env(app["env"])
    if problems:
        die(EXIT_CONFIG, "env vars missing", problems="; ".join(problems))
    cfg = app["config"]
    subdomain = cfg_str(cfg, "bamboohr_subdomain")
    photo_size = cfg_photo_size(cfg)
    dry_run = cfg_bool(cfg, "dry_run")
    api_key = os.environ.get("PDT_BAMBOOHR_API_KEY", "").strip()

    log("info", "starting", subdomain=subdomain, photo_size=photo_size, dry_run=dry_run)
    token = graph_token(EXIT_ENTRA)
    log("info", "Entra token acquired")

    bamboo_rows = fetch_bamboo_employees(subdomain, api_key)
    log("info", "fetched BambooHR employees", total=len(bamboo_rows))
    candidates = employees_with_photos(bamboo_rows)
    entra_rows = fetch_entra_users(token)
    entra_index = index_entra_by_email(entra_rows)

    uploaded = 0
    unchanged = 0
    skipped = 0
    for employee in candidates:
        matches = entra_index.get(employee["normalized_email"], [])
        if len(matches) == 0:
            log("warning", "no Entra user matched workEmail",
                employee_id=employee["id"], email=employee["email"])
            skipped += 1
            continue
        if len(matches) > 1:
            log("warning", "multiple Entra users matched workEmail; skipped",
                employee_id=employee["id"], email=employee["email"], matches=len(matches))
            skipped += 1
            continue
        entra_user = matches[0]
        if entra_user.get("accountEnabled") is not True:
            log("warning", "matched Entra user is disabled; skipped",
                employee_id=employee["id"], entra_id=entra_user.get("id"))
            skipped += 1
            continue
        entra_id = str(entra_user.get("id") or "")
        photo = fetch_bamboo_photo(subdomain, api_key, employee["id"], photo_size)
        if photo is None:
            log("warning", "BambooHR photo missing after isPhotoUploaded; skipped",
                employee_id=employee["id"])
            skipped += 1
            continue
        if len(photo) > GRAPH_PHOTO_MAX_BYTES:
            log("warning", "BambooHR photo exceeds Graph 4 MB limit; skipped",
                employee_id=employee["id"], bytes=len(photo))
            skipped += 1
            continue
        current = fetch_entra_photo(token, entra_id)
        if current is not None and photo_hash(current) == photo_hash(photo):
            unchanged += 1
            continue
        if dry_run:
            log("info", "would update Entra photo",
                employee_id=employee["id"], entra_id=entra_id, email=employee["email"])
            uploaded += 1
            continue
        put_entra_photo(token, entra_id, photo)
        log("info", "updated Entra photo",
            employee_id=employee["id"], entra_id=entra_id, email=employee["email"])
        uploaded += 1

    log(
        "info",
        "photo sync finished",
        bamboo_fetched=len(bamboo_rows),
        with_photos=len(candidates),
        entra_fetched=len(entra_rows),
        uploaded=uploaded,
        unchanged=unchanged,
        skipped=skipped,
        dry_run=dry_run,
    )
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
