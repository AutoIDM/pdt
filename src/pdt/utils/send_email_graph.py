from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path

from pdt.utils import email_auth
from pdt.utils.log import die, log

EXIT_CONFIG = 1
EXIT_EMAIL = 3

USER_ENV = "PDT_GRAPH_MAIL_USER"
CLIENT_ID_ENV = "PDT_GRAPH_MAIL_CLIENT_ID"
TENANT_ID_ENV = "PDT_GRAPH_MAIL_TENANT_ID"
CACHE_ENV = "PDT_GRAPH_MAIL_CACHE_B64"
VARS = (USER_ENV, CLIENT_ID_ENV, TENANT_ID_ENV, CACHE_ENV)
SCOPE = "https://graph.microsoft.com/Mail.Send"
CACHE_PURPOSE = "graph"
SEND_URL = "https://graph.microsoft.com/v1.0/me/sendMail"
MAIL_SEND_PERMISSION = "Microsoft Graph delegated Mail.Send"

_ACCESS_TOKENS = {}


def optional_env(name: str) -> str:
    return os.environ.get(name, "").strip()


def config_problems(check_oauth: bool = True,
                    check_authorization: bool = False) -> list[str]:
    problems = []
    if optional_env(USER_ENV) == "":
        problems.append(f"missing required env var {USER_ENV}")
    elif check_oauth:
        if optional_env(CLIENT_ID_ENV) == "":
            problems.append(f"missing required env var {CLIENT_ID_ENV}")
        elif check_authorization and optional_env(CACHE_ENV) == "":
            problems.append(
                "Microsoft Graph OAuth authorization is incomplete; "
                "run or deploy the app from a terminal")
    return problems


def client_missing() -> bool:
    if not any(optional_env(name) != "" for name in VARS):
        return False
    if optional_env(USER_ENV) == "":
        return False
    return optional_env(CLIENT_ID_ENV) == ""


def setup_help(env_file: Path | None) -> list[str]:
    where = f"in {env_file}" if env_file is not None else "in the environment"
    return [
        "PDT_GRAPH_MAIL_USER is set, so PDT will send with Microsoft Graph Mail.Send.",
        f"Microsoft Graph needs one more value {where}:",
        f"  {CLIENT_ID_ENV}=",
        "Get it: create an app registration at https://entra.microsoft.com/ "
        "and copy its Application (client) ID.",
        "Guide: README.md, section \"Microsoft Graph\".",
    ]


def prompt_client(env_file: Path) -> bool:
    print()
    print("Email provider: Microsoft Graph")
    print("Authentication: OAuth 2.0")
    print("Status: OAuth client setup required")
    print()
    for line in setup_help(env_file):
        print(line)
    print()
    print("Paste the value now, or press Enter to stop.")
    value = email_auth._ask(f"{CLIENT_ID_ENV}: ")
    if value == "":
        return False
    email_auth._write_env(env_file, CLIENT_ID_ENV, value)
    os.environ[CLIENT_ID_ENV] = value
    print(f"Saved {CLIENT_ID_ENV} in {env_file}.")
    return True


def sender_mismatch(email_from: str) -> str | None:
    user = optional_env(USER_ENV)
    from_addr = email_from.strip()
    if from_addr == "" or user == "":
        return None
    if from_addr.casefold() == user.casefold():
        return None
    return (
        f"email_from must match {USER_ENV} "
        f"(Graph sends as /me/sendMail; Mail.Send.Shared is not supported)"
    )


def mail_json(subject: str, body: str, html: str, to_addrs: list) -> dict:
    if html != "":
        content_type = "HTML"
        content = html
    else:
        content_type = "Text"
        content = body
    return {
        "message": {
            "subject": subject,
            "body": {"contentType": content_type, "content": content},
            "toRecipients": [
                {"emailAddress": {"address": addr}} for addr in to_addrs
            ],
        }
    }


def _confirm(user: str) -> None:
    print()
    print("Email provider: Microsoft Graph")
    print(f"Account: {user}")
    print("Authentication: OAuth 2.0")
    print("Status: Authorization required")
    print()
    print("PDT will request Microsoft Graph delegated Mail.Send permission.")
    print("PDT will not request permission to read email.")
    print()
    try:
        answer = input("Open Microsoft sign-in now? [Y/n] ").strip().lower()
    except EOFError:
        answer = "n"
    if answer not in ("", "y", "yes"):
        raise email_auth.OAuthError("OAuth authorization was not completed")


def get_access_token(env_file: Path | None = None,
                     interactive: bool | None = None) -> str:
    user = optional_env(USER_ENV)
    client_id = optional_env(CLIENT_ID_ENV)
    tenant_id = optional_env(TENANT_ID_ENV) or "common"
    cache_b64 = optional_env(CACHE_ENV)
    problems = config_problems(check_oauth=True)
    if problems:
        raise email_auth.OAuthError("; ".join(problems))
    key = (CACHE_PURPOSE, user.casefold(), client_id)
    if key in _ACCESS_TOKENS:
        return _ACCESS_TOKENS[key]
    cache_data = email_auth._decode_cache(
        cache_b64, CACHE_PURPOSE, user, client_id, CACHE_ENV)
    interactive_ok = email_auth.can_prompt(interactive)
    token = _microsoft_access_token(
        user, client_id, tenant_id, cache_data, env_file, interactive_ok)
    _ACCESS_TOKENS[key] = token
    return token


def _microsoft_access_token(user: str, client_id: str, tenant_id: str,
                            cache_data: dict, env_file: Path | None,
                            interactive: bool) -> str:
    import msal
    cache = msal.SerializableTokenCache()
    serialized = str(cache_data.get("msal") or "")
    if serialized != "":
        try:
            cache.deserialize(serialized)
        except Exception as e:
            raise email_auth.OAuthError(
                f"Microsoft Graph OAuth cache is invalid; "
                f"remove {CACHE_ENV} and try again") from e
    app = msal.PublicClientApplication(
        client_id,
        authority=f"https://login.microsoftonline.com/{tenant_id}",
        token_cache=cache,
    )
    accounts = app.get_accounts(username=user)
    result = None
    old_refresh_tokens = list(cache.search(
        msal.TokenCache.CredentialType.REFRESH_TOKEN))
    if accounts:
        result = app.acquire_token_silent([SCOPE], account=accounts[0])
    if result and "access_token" in result:
        new_refresh_tokens = list(cache.search(
            msal.TokenCache.CredentialType.REFRESH_TOKEN))
        if new_refresh_tokens != old_refresh_tokens:
            email_auth._save_cache(env_file, {
                "client_id": client_id,
                "msal": cache.serialize(),
                "provider": CACHE_PURPOSE,
                "user": user,
            }, CACHE_ENV)
        return result["access_token"]
    if not interactive:
        raise email_auth.OAuthError(
            "Microsoft Graph authorization is required; "
            "run or deploy the app from a terminal")
    _confirm(user)
    try:
        result = app.acquire_token_interactive(
            [SCOPE],
            login_hint=user,
            prompt="select_account",
        )
    except Exception as e:
        raise email_auth.OAuthError(
            f"Microsoft Graph authorization failed: {e}")
    if "access_token" not in result:
        detail = result.get("error_description") or result.get("error") or "unknown error"
        raise email_auth.OAuthError(
            f"Microsoft Graph authorization failed: {detail}")
    email_auth._save_cache(env_file, {
        "client_id": client_id,
        "msal": cache.serialize(),
        "provider": CACHE_PURPOSE,
        "user": user,
    }, CACHE_ENV)
    account = (result.get("id_token_claims") or {}).get("preferred_username") or user
    print(f"Connected Microsoft Graph for {account}.")
    return result["access_token"]


def prepare(env_file: Path | None = None,
            interactive: bool | None = None) -> None:
    try:
        get_access_token(env_file, interactive)
    except email_auth.OAuthError as e:
        die(EXIT_CONFIG, "graph oauth failed", error=str(e))


def _mail_send_fix() -> tuple:
    return (
        f"Microsoft Graph denied the send. Required permission: {MAIL_SEND_PERMISSION}.",
        "Fix: Entra admin center > App registrations > API permissions > "
        "Microsoft Graph > Delegated permissions > Mail.Send.",
        "Grant admin consent if the tenant requires it. Authenticated SMTP is not used.",
    )


def send_mail(from_addr: str, to_addrs: list, subject: str, body: str,
              html: str = "") -> None:
    user = optional_env(USER_ENV)
    if user == "":
        die(EXIT_CONFIG, "missing env var", name=USER_ENV)
    mismatch = sender_mismatch(from_addr)
    if mismatch is not None:
        die(EXIT_CONFIG, mismatch)
    token = get_access_token()
    payload = json.dumps(mail_json(subject, body, html, to_addrs)).encode()
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "User-Agent": "pdt/1.0",
    }
    req = urllib.request.Request(
        SEND_URL, data=payload, method="POST", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            if resp.status != 202:
                die(EXIT_EMAIL, "graph send failed", status=resp.status)
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")[:800]
        if e.code == 403 or "ErrorAccessDenied" in detail:
            for line in _mail_send_fix():
                log("error", line)
            die(EXIT_EMAIL, "graph send denied", status=e.code, detail=detail)
        die(EXIT_EMAIL, "graph send failed", status=e.code, detail=detail)
    except urllib.error.URLError as e:
        die(EXIT_EMAIL, "graph connection failed", error=str(e.reason))
