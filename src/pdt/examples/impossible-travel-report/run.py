#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["pdt-cli[apps]==PDT_VERSION"]
# ///
"""Impossible Travel Report checks Entra ID sign-in logs for sign-ins from two far-apart places.

Pulls recent successful interactive logins from Microsoft Graph, then
alerts if the same user signed in from two far-apart places inside the
lookback window.

Needs an app registration with AuditLog.Read.All (application) and
admin consent, in a tenant with Entra ID P1 or P2 (without P1/P2,
Graph has no sign-in logs).

Env (creds only — put these in .env at this folder or any parent):
  Always:
    PDT_AZURE_TENANT_ID, PDT_AZURE_CLIENT_ID
  One of:
    PDT_AZURE_CLIENT_SECRET
    PDT_AZURE_PRIVATE_KEY_B64   (base64 of the PFX; what deploy sets in the cloud)
    PDT_AZURE_PRIVATE_KEY_PATH  (+ optional PDT_AZURE_PRIVATE_KEY_PASSWORD)
  Cert auth wins over the client secret, and B64 wins over PATH.
  A relative PATH is relative to the repo root.
  Email: see pdt.utils.send_email (exactly one of smtp / ses / resend).
  If no email transport is set, findings print to stdout.

Exit codes: 0 ok, 1 bad config, 2 Entra/Graph failure, 3 email failure.
"""

from __future__ import annotations

import math
import sys
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path

from pdt.config import ConfigError, check_env, load_env, merged_app
from pdt.utils.entra import graph_pages, graph_token
from pdt.utils.log import die, log
from pdt.utils.send_email import pick_transport, send_email

EXIT_OK = 0
EXIT_CONFIG = 1
EXIT_ENTRA = 2

GRAPH_SIGNINS = "https://graph.microsoft.com/v1.0/auditLogs/signIns"


def cfg_float(cfg: dict, key: str) -> float:
    raw = str(cfg.get(key, "") or "").strip()
    if raw == "":
        die(EXIT_CONFIG, "config.yml missing key", key=key)
    try:
        return float(raw)
    except ValueError:
        die(EXIT_CONFIG, "config.yml value is not a number", key=key, value=raw)


def fetch_signins(token: str, since: datetime) -> list:
    # Graph times out on unscoped sign-in queries, so the time filter is required.
    params = urllib.parse.urlencode({
        "$filter": (
            f"createdDateTime ge {since:%Y-%m-%dT%H:%M:%SZ}"
            " and status/errorCode eq 0 and isInteractive eq true"
        ),
        "$select": (
            "createdDateTime,userPrincipalName,userDisplayName,"
            "isInteractive,ipAddress,status,location"
        ),
        "$top": "999",
    })
    logins = []
    fetched = 0
    for rows in graph_pages(token, f"{GRAPH_SIGNINS}?{params}", EXIT_ENTRA):
        fetched += len(rows)
        for raw in rows:
            login = usable_login(raw)
            if login is not None:
                logins.append(login)
        log("info", "fetched sign-in page", total=fetched, usable=len(logins))
    return logins


def parse_ts(raw: str | None) -> datetime | None:
    try:
        ts = datetime.fromisoformat(raw or "")
    except ValueError:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts


def usable_login(raw: dict) -> dict | None:
    """Trim one Graph sign-in row down to the fields the check needs.

    Returns None for rows the check must skip: failures, non-interactive
    token refreshes (their IPs hop and look like teleporting), and rows
    with no user, no timestamp, or no coordinates.
    """
    status = raw.get("status") or {}
    if status.get("errorCode") != 0 or raw.get("isInteractive") is False:
        return None
    loc = raw.get("location") or {}
    geo = loc.get("geoCoordinates") or {}
    upn = raw.get("userPrincipalName") or ""
    ts = parse_ts(raw.get("createdDateTime"))
    if upn == "" or ts is None or geo.get("latitude") is None or geo.get("longitude") is None:
        return None
    parts = [loc.get("city"), loc.get("state"), loc.get("countryOrRegion")]
    return {
        "upn": upn,
        "name": raw.get("userDisplayName") or "",
        "ts": ts,
        "lat": float(geo["latitude"]),
        "lon": float(geo["longitude"]),
        "ip": raw.get("ipAddress") or "ip unknown",
        "place": ", ".join(p for p in parts if p) or "unknown",
    }


def distance_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle (haversine) distance between two lat/lon points."""
    EARTH_KM = 6371
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlon / 2) ** 2
    return 2 * EARTH_KM * math.asin(math.sqrt(a))


def find_impossible(logins: list, min_distance_km: float) -> list:
    """Sort each user's logins by time; flag consecutive pairs at least min_distance_km apart."""
    by_user = {}
    for login in logins:
        by_user.setdefault(login["upn"], []).append(login)

    hits = []
    for rows in by_user.values():
        rows.sort(key=lambda r: r["ts"])
        for first, second in zip(rows, rows[1:]):
            km = distance_km(first["lat"], first["lon"], second["lat"], second["lon"])
            if km < min_distance_km:
                continue
            hours = (second["ts"] - first["ts"]).total_seconds() / 3600
            hits.append({
                "upn": first["upn"],
                "name": second["name"] or first["name"],
                "km": round(km, 1),
                "hours": round(hours, 2),
                "first": first,
                "second": second,
            })
    return hits


def format_ts(ts: datetime) -> str:
    return f"{ts.astimezone(timezone.utc):%Y-%m-%d %H:%M} UTC"


def format_body(hits: list) -> str:
    lines = ["Impossible login: the same account signed in from two places too far apart.", ""]
    for hit in hits:
        first, second = hit["first"], hit["second"]
        who = hit["upn"]
        if hit["name"] != "":
            who = f"{who} ({hit['name']})"
        lines.append(who)
        lines.append(f"  {hit['km']} km apart, {hit['hours']} hours between sign-ins")
        lines.append(f"  {format_ts(first['ts'])}  {first['place']}  ({first['ip']})")
        lines.append(f"  {format_ts(second['ts'])}  {second['place']}  ({second['ip']})")
        lines.append("")
    return "\n".join(lines)


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

    lookback_hours = cfg_float(cfg, "lookback_hours")
    min_distance_km = cfg_float(cfg, "min_distance_km")

    # `pdt validate|run|deploy` check the email config before this runs.
    transport = pick_transport()
    email_subject = str(cfg.get("email_subject") or "Impossible login detected").strip()
    email_to = cfg.get("email_to") or ""
    email_from = str(cfg.get("email_from") or "").strip()

    since = datetime.now(timezone.utc) - timedelta(hours=lookback_hours)
    log("info", "starting", lookback_hours=lookback_hours, min_distance_km=min_distance_km,
        since=f"{since:%Y-%m-%dT%H:%M:%SZ}", email=transport)

    token = graph_token(EXIT_ENTRA)
    log("info", "entra token acquired")

    logins = fetch_signins(token, since)
    log("info", "usable logins", count=len(logins))

    hits = find_impossible(logins, min_distance_km)
    if len(hits) == 0:
        log("info", "no impossible logins found")
        return EXIT_OK
    for hit in hits:
        log("warning", "impossible login", user=hit["upn"], km=hit["km"], hours=hit["hours"],
            from_place=hit["first"]["place"], to_place=hit["second"]["place"])

    send_email(email_from, email_to, email_subject, format_body(hits))
    if transport == "stdout":
        log("info", "email not configured; findings printed to stdout", count=len(hits))
    else:
        log("info", "alert sent", transport=transport, to=email_to, count=len(hits))
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
