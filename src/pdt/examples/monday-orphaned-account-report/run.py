#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["pdt-cli[apps]==PDT_VERSION"]
# ///
"""Report active Monday users with no active Entra ID account.

Monday users are active only when the API returns ``status: ACTIVE``. Their
trimmed, case-insensitive email addresses are compared with active Entra
``userPrincipalName`` values.

Needs a Monday personal/API token and an Entra app registration with
User.Read.All (application) plus admin consent.

Env (creds only -- put these in .env at this folder or any parent):
  Always:
    PDT_MONDAY_API_TOKEN
    PDT_AZURE_TENANT_ID, PDT_AZURE_CLIENT_ID
  One of:
    PDT_AZURE_CLIENT_SECRET
    PDT_AZURE_PRIVATE_KEY_B64
    PDT_AZURE_PRIVATE_KEY_PATH (+ optional PDT_AZURE_PRIVATE_KEY_PASSWORD)
  Certificate auth wins over client-secret auth, and B64 wins over PATH.
  Email: see pdt.utils.send_email. With no email transport, findings go to stdout.

Exit codes: 0 ok, 1 bad config, 2 Entra/Graph failure, 3 email failure,
4 Monday API failure.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.parse
from datetime import datetime, timezone
from html import escape
from pathlib import Path

from pdt.config import ConfigError, check_env, load_env, merged_app
from pdt.utils.entra import graph_pages, graph_token
from pdt.utils.log import die, log
from pdt.utils.send_email import pick_transport, send_email
from pdt.utils.web import http_json

EXIT_OK = 0
EXIT_CONFIG = 1
EXIT_ENTRA = 2
EXIT_MONDAY = 4

GRAPH_USERS = "https://graph.microsoft.com/v1.0/users"
ENTRA_USERS_PORTAL = (
    "https://entra.microsoft.com/#view/Microsoft_AAD_UsersAndTenants"
    "/UserManagementMenuBlade/~/AllUsers"
)
MONDAY_API = "https://api.monday.com/v2"
MONDAY_API_VERSION = "2026-07"

MONDAY_USERS_QUERY = """
query ActiveUsers($limit: Int!, $page: Int!) {
  users(
    limit: $limit
    page: $page
    status: [ACTIVE]
    sort: [{ field: CREATED_AT, direction: ASC }]
  ) {
    id
    name
    email
    status
    url
  }
}
""".strip()


def cfg_int(cfg: dict, key: str, minimum: int, maximum: int) -> int:
    raw = str(cfg.get(key, "") or "").strip()
    if raw == "":
        die(EXIT_CONFIG, "config.yml missing key", key=key)
    try:
        value = int(raw)
    except ValueError:
        die(EXIT_CONFIG, "config.yml value is not an integer", key=key, value=raw)
    if not minimum <= value <= maximum:
        die(EXIT_CONFIG, "config.yml value is out of range",
            key=key, value=value, minimum=minimum, maximum=maximum)
    return value


def fetch_monday_users(token: str, page_size: int) -> list[dict]:
    users = []
    page = 1
    while True:
        request_body = json.dumps({
            "query": MONDAY_USERS_QUERY,
            "variables": {"limit": page_size, "page": page},
        }).encode()
        payload = http_json(
            MONDAY_API,
            EXIT_MONDAY,
            method="POST",
            headers={
                "Authorization": token,
                "API-Version": MONDAY_API_VERSION,
                "Content-Type": "application/json",
                "User-Agent": "pdt/1.0",
            },
            data=request_body,
        )
        errors = payload.get("errors")
        if errors:
            messages = []
            for error in errors:
                if isinstance(error, dict):
                    messages.append(str(error.get("message") or "unknown GraphQL error"))
                else:
                    messages.append(str(error))
            die(EXIT_MONDAY, "Monday GraphQL request failed", errors="; ".join(messages))
        rows = (payload.get("data") or {}).get("users")
        if not isinstance(rows, list):
            die(EXIT_MONDAY, "Monday response had no data.users list", page=page)
        users.extend(rows)
        log("info", "fetched Monday user page", page=page, total=len(users))
        if len(rows) < page_size:
            return users
        page += 1


def fetch_entra_users(token: str) -> list[dict]:
    params = urllib.parse.urlencode({
        "$select": "id,displayName,userPrincipalName,accountEnabled",
        "$filter": "accountEnabled eq true",
        "$top": "999",
    })
    users = []
    for rows in graph_pages(token, f"{GRAPH_USERS}?{params}", EXIT_ENTRA):
        users.extend(rows)
        log("info", "fetched Entra user page", total=len(users))
    return users


def normalize_identity(value: str | None) -> str:
    return str(value or "").strip().casefold()


def active_monday_users(rows: list[dict]) -> list[dict]:
    users = []
    for row in rows:
        if row.get("status") != "ACTIVE":
            continue
        raw_email = str(row.get("email") or "").strip()
        email = normalize_identity(raw_email)
        if email == "":
            log("warning", "active Monday user has no email; skipped", id=row.get("id"))
            continue
        users.append({
            "id": str(row.get("id") or ""),
            "name": str(row.get("name") or "").strip(),
            "email": raw_email,
            "normalized_email": email,
            "url": str(row.get("url") or "").strip(),
        })
    return users


def active_entra_upns(rows: list[dict]) -> set[str]:
    upns = set()
    for row in rows:
        if row.get("accountEnabled") is not True:
            continue
        upn = normalize_identity(row.get("userPrincipalName"))
        if upn != "":
            upns.add(upn)
    return upns


def find_orphaned(monday_active: list[dict], active_upns: set[str]) -> list[dict]:
    orphaned = [
        user for user in monday_active
        if user["normalized_email"] not in active_upns
    ]
    return sorted(orphaned, key=lambda user: (user["normalized_email"], user["id"]))


def generated_at() -> str:
    return f"{datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC"


def format_body(users: list[dict], monday_checked: int, entra_checked: int) -> str:
    noun = "account" if len(users) == 1 else "accounts"
    lines = [
        "Monday account review",
        "",
        f"{len(users)} active Monday {noun} did not match an active Microsoft",
        "Entra ID user by primary sign-in name.",
        "",
        "Review these accounts before you disable or delete them. A person may",
        "use an email alias that differs from their Entra sign-in name.",
        "",
    ]
    for number, user in enumerate(users, start=1):
        lines.append(f"{number}. {user['name'] or user['email']}")
        if user["name"] != "":
            lines.append(f"   Monday email: {user['email']}")
        lines.append(f"   Monday user ID: {user['id'] or 'unknown'}")
        if user["url"] != "":
            lines.append(f"   Monday profile: {user['url']}")
        lines.append("")
    lines.append("Summary")
    lines.append(f"Active Monday accounts checked: {monday_checked}")
    lines.append(f"Active Entra users checked: {entra_checked}")
    lines.append(f"Accounts requiring review: {len(users)}")
    lines.append("")
    lines.append("Match rule")
    lines.append("The report compares Monday email addresses with Entra")
    lines.append("userPrincipalName values. It ignores capitalization and")
    lines.append("surrounding spaces.")
    lines.append("")
    lines.append(f"Search an address in Entra ID: {ENTRA_USERS_PORTAL}")
    lines.append(f"Generated: {generated_at()}")
    return "\n".join(lines)


def format_html(users: list[dict], monday_checked: int, entra_checked: int) -> str:
    noun = "account" if len(users) == 1 else "accounts"
    parts = [
        "<html><body style=\"font-family: system-ui, sans-serif; font-size: 14px;\">",
        "<h2 style=\"margin: 0 0 12px;\">Monday account review</h2>",
        f"<p>{len(users)} active Monday {noun} did not match an active Microsoft"
        " Entra ID user by primary sign-in name.</p>",
        "<p>Review these accounts before you disable or delete them. A person may"
        " use an email alias that differs from their Entra sign-in name.</p>",
        "<ol>",
    ]
    for user in users:
        name = escape(user["name"] or user["email"])
        user_id = escape(user["id"] or "unknown")
        if user["url"] != "":
            user_id = f'<a href="{escape(user["url"])}">{user_id}</a>'
        detail = f"Monday user ID {user_id}"
        if user["name"] != "":
            detail = f"{escape(user['email'])} &mdash; {detail}"
        parts.append(f"<li><strong>{name}</strong><br>{detail}</li>")
    parts.append("</ol>")
    parts.append("<h3 style=\"margin: 16px 0 4px;\">Summary</h3>")
    parts.append("<ul>")
    parts.append(f"<li>Active Monday accounts checked: {monday_checked}</li>")
    parts.append(f"<li>Active Entra users checked: {entra_checked}</li>")
    parts.append(f"<li>Accounts requiring review: {len(users)}</li>")
    parts.append("</ul>")
    parts.append("<h3 style=\"margin: 16px 0 4px;\">Match rule</h3>")
    parts.append("<p>The report compares Monday email addresses with Entra"
                 " userPrincipalName values. It ignores capitalization and"
                 " surrounding spaces.</p>")
    parts.append(f'<p><a href="{ENTRA_USERS_PORTAL}">Search an address in Entra ID</a></p>')
    parts.append(f"<p style=\"color: #666;\">Generated: {generated_at()}</p>")
    parts.append("</body></html>")
    return "\n".join(parts)


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
    page_size = cfg_int(cfg, "monday_page_size", 1, 1000)

    transport = pick_transport()
    email_subject = str(
        cfg.get("email_subject") or "Monday users missing from Entra ID"
    ).strip()
    email_to = cfg.get("email_to") or ""
    email_from = str(cfg.get("email_from") or "").strip()

    log("info", "starting", monday_page_size=page_size, email=transport)
    token = graph_token(EXIT_ENTRA)
    log("info", "Entra token acquired")

    monday_rows = fetch_monday_users(os.environ.get("PDT_MONDAY_API_TOKEN", "").strip(), page_size)
    entra_rows = fetch_entra_users(token)
    monday_active = active_monday_users(monday_rows)
    active_upns = active_entra_upns(entra_rows)
    orphaned = find_orphaned(monday_active, active_upns)
    log(
        "info",
        "accounts compared",
        monday_fetched=len(monday_rows),
        monday_active=len(monday_active),
        entra_fetched=len(entra_rows),
        entra_active_upns=len(active_upns),
        orphaned=len(orphaned),
    )
    if not orphaned:
        log("info", "no orphaned Monday accounts found")
        return EXIT_OK

    for user in orphaned:
        log(
            "warning",
            "orphaned Monday account",
            monday_user_id=user["id"],
            email=user["email"],
            name=user["name"],
        )
    send_email(email_from, email_to, email_subject,
               format_body(orphaned, len(monday_active), len(active_upns)),
               format_html(orphaned, len(monday_active), len(active_upns)))
    if transport == "stdout":
        log(
            "info",
            "email not configured; findings printed to stdout",
            count=len(orphaned),
        )
    else:
        log("info", "report sent", transport=transport, to=email_to, count=len(orphaned))
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
