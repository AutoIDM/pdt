"""OAuth 2.0 authentication for SMTP providers."""

from __future__ import annotations

import base64
import json
import os
import sys
from pathlib import Path


GOOGLE_HOSTS = {"smtp.gmail.com"}
MICROSOFT_HOSTS = {"smtp.office365.com", "smtp-mail.outlook.com"}
GOOGLE_SCOPE = "https://mail.google.com/"
MICROSOFT_SCOPE = "https://outlook.office.com/SMTP.Send"
CACHE_ENV = "PDT_SMTP_OAUTH_CACHE_B64"

_ACCESS_TOKENS = {}


class OAuthError(Exception):
    pass


def provider_for_host(host: str) -> str | None:
    normalized = host.strip().lower().rstrip(".")
    if normalized in GOOGLE_HOSTS:
        return "google"
    if normalized in MICROSOFT_HOSTS:
        return "microsoft"
    return None


def smtp_auth_method(host: str, password: str, configured: str) -> str:
    method = configured.strip().lower()
    if method == "":
        if password != "":
            return "password"
        if provider_for_host(host) is not None:
            return "oauth2"
        raise OAuthError(
            "set PDT_SMTP_PASSWORD or PDT_SMTP_AUTH=oauth2")
    if method not in ("password", "oauth2"):
        raise OAuthError(
            "PDT_SMTP_AUTH must be password or oauth2")
    if method == "password" and password == "":
        raise OAuthError(
            "PDT_SMTP_PASSWORD is required for password authentication")
    if method == "oauth2" and provider_for_host(host) is None:
        raise OAuthError(
            "SMTP OAuth supports smtp.gmail.com, smtp.office365.com, and smtp-mail.outlook.com")
    return method


def oauth_config_errors(host: str, client_id: str,
                        client_secret: str, cache_b64: str,
                        check_authorization: bool = False) -> list[str]:
    provider = provider_for_host(host)
    if provider is None:
        return [
            "SMTP OAuth supports smtp.gmail.com, smtp.office365.com, and smtp-mail.outlook.com"
        ]
    problems = []
    if client_id == "":
        problems.append("missing required env var PDT_SMTP_OAUTH_CLIENT_ID")
    if provider == "google" and client_secret == "":
        problems.append("missing required env var PDT_SMTP_OAUTH_CLIENT_SECRET")
    if check_authorization and cache_b64 == "":
        name = "Google Gmail" if provider == "google" else "Microsoft"
        problems.append(
            f"{name} OAuth authorization is incomplete; run or deploy the app from a terminal")
    return problems


def _provider_name(provider: str) -> str:
    return "Google Gmail" if provider == "google" else "Microsoft"


def oauth_setup_help(host: str, configured_auth: str,
                     env_file: Path | None) -> list[str]:
    """Explain why OAuth applies and where the missing client values come from."""
    provider = provider_for_host(host)
    name = _provider_name(provider)
    if configured_auth.strip().lower() == "oauth2":
        reason = f"PDT_SMTP_AUTH is oauth2, so PDT will sign in with {name} OAuth."
    else:
        reason = (f"PDT_SMTP_HOST is {host} and PDT_SMTP_PASSWORD is empty, "
                  f"so PDT will sign in with {name} OAuth.")
    where = f"in {env_file}" if env_file is not None else "in the environment"
    if provider == "google":
        return [
            reason,
            f"Google OAuth needs two more values {where}:",
            "  PDT_SMTP_OAUTH_CLIENT_ID=",
            "  PDT_SMTP_OAUTH_CLIENT_SECRET=",
            "Get them: create a \"Desktop app\" OAuth client at "
            "https://console.cloud.google.com/auth/clients and copy the client ID and client secret.",
            "Simpler option: set PDT_SMTP_PASSWORD to a Gmail App Password "
            "from https://myaccount.google.com/apppasswords instead.",
            "Guide: README.md, section \"Google OAuth\".",
        ]
    return [
        reason,
        f"Microsoft OAuth needs one more value {where}:",
        "  PDT_SMTP_OAUTH_CLIENT_ID=",
        "Get it: create an app registration at https://entra.microsoft.com/ "
        "and copy its Application (client) ID.",
        "Guide: README.md, section \"Microsoft OAuth\".",
    ]


def _ask(prompt: str) -> str:
    try:
        return input(prompt).strip()
    except EOFError:
        return ""


def prompt_oauth_client(host: str, configured_auth: str, client_id: str,
                        client_secret: str, env_file: Path) -> bool:
    """Ask for the missing OAuth client values and save them in env_file."""
    provider = provider_for_host(host)
    print()
    print(f"Email provider: {_provider_name(provider)}")
    print("Authentication: OAuth 2.0")
    print("Status: OAuth client setup required")
    print()
    for line in oauth_setup_help(host, configured_auth, env_file):
        print(line)
    print()
    print("Paste the values now, or press Enter to stop.")
    values = {}
    if client_id == "":
        values["PDT_SMTP_OAUTH_CLIENT_ID"] = _ask("OAuth client ID: ")
    if provider == "google" and client_secret == "":
        values["PDT_SMTP_OAUTH_CLIENT_SECRET"] = _ask("OAuth client secret: ")
    if any(value == "" for value in values.values()):
        return False
    for name, value in values.items():
        _write_env(env_file, name, value)
        os.environ[name] = value
    print(f"Saved {', '.join(values)} in {env_file}.")
    return True


def xoauth2_response(user: str, access_token: str) -> str:
    return f"user={user}\x01auth=Bearer {access_token}\x01\x01"


def _decode_cache(raw: str, provider: str, user: str,
                  client_id: str, cache_env: str = CACHE_ENV) -> dict:
    if raw == "":
        return {}
    try:
        data = json.loads(base64.b64decode(raw, validate=True))
    except (ValueError, json.JSONDecodeError):
        raise OAuthError(
            f"{cache_env} is invalid; remove it and run the app again")
    if not isinstance(data, dict):
        raise OAuthError(
            f"{cache_env} is invalid; remove it and run the app again")
    if data.get("provider") != provider:
        return {}
    if str(data.get("user") or "").casefold() != user.casefold():
        return {}
    if data.get("client_id") != client_id:
        return {}
    return data


def _save_cache(env_file: Path | None, data: dict,
                cache_env: str = CACHE_ENV) -> None:
    raw = json.dumps(data, separators=(",", ":"), sort_keys=True).encode()
    value = base64.b64encode(raw).decode()
    os.environ[cache_env] = value
    if env_file is None:
        _save_cloud_cache(value, cache_env)
        return
    _write_env(env_file, cache_env, value)
    print(f"Saved email authorization in {env_file}.")


def _write_env(env_file: Path, name: str, value: str) -> None:
    from dotenv import set_key
    try:
        env_file.parent.mkdir(parents=True, exist_ok=True)
        env_file.touch(mode=0o600, exist_ok=True)
        set_key(str(env_file), name, value, quote_mode="never")
    except OSError as e:
        raise OAuthError(f"could not save {name} in {env_file}: {e}")


def _save_cloud_cache(value: str, cache_env: str = CACHE_ENV) -> None:
    resource = os.environ.get("PDT_ENV_SECRET_RESOURCE", "").strip()
    raw = os.environ.get("PDT_ENV_JSON", "").strip()
    if resource == "" or raw == "":
        return
    try:
        values = json.loads(raw)
        values[cache_env] = value
        payload = json.dumps(values, separators=(",", ":"), sort_keys=True)
        from google.cloud import secretmanager
        client = secretmanager.SecretManagerServiceClient()
        client.add_secret_version(request={
            "parent": resource,
            "payload": {"data": payload.encode()},
        })
    except Exception as e:
        raise OAuthError(
            f"could not update OAuth authorization in {resource}: {e}")
    os.environ["PDT_ENV_JSON"] = payload


def can_prompt(interactive: bool | None) -> bool:
    if interactive is not None:
        return interactive
    if os.environ.get("CLOUD_RUN_JOB", "").strip() != "":
        return False
    return sys.stdin.isatty() and sys.stdout.isatty()


def _confirm(provider: str, user: str) -> None:
    name = "Google Gmail" if provider == "google" else "Microsoft"
    print()
    print(f"Email provider: {name}")
    print(f"Account: {user}")
    print("Authentication: OAuth 2.0")
    print("Status: Authorization required")
    print()
    if provider == "google":
        print("Google requires full Gmail permission for SMTP OAuth.")
        print("PDT will use this permission only to send notification email.")
    else:
        print("PDT will request permission to send email through SMTP.")
        print("PDT will not request permission to read email.")
    print()
    try:
        answer = input(f"Open {name} sign-in now? [Y/n] ").strip().lower()
    except EOFError:
        answer = "n"
    if answer not in ("", "y", "yes"):
        raise OAuthError("OAuth authorization was not completed")


def _google_authorize(user: str, client_id: str,
                      client_secret: str) -> tuple[str, str]:
    from google_auth_oauthlib.flow import InstalledAppFlow
    config = {
        "installed": {
            "client_id": client_id,
            "client_secret": client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": ["http://localhost"],
        }
    }
    flow = InstalledAppFlow.from_client_config(config, [GOOGLE_SCOPE])
    try:
        credentials = flow.run_local_server(
            port=0,
            access_type="offline",
            prompt="consent",
            login_hint=user,
            authorization_prompt_message="Opening Google sign-in in your browser...",
            success_message="Google authorization completed. You can close this window.",
        )
    except Exception as e:
        raise OAuthError(f"Google authorization failed: {e}")
    if not credentials.refresh_token:
        raise OAuthError(
            "Google did not return a refresh token; revoke PDT access and try again")
    return credentials.token, credentials.refresh_token


def _google_access_token(user: str, client_id: str, client_secret: str,
                         cache: dict, env_file: Path | None,
                         interactive: bool) -> str:
    refresh_token = str(cache.get("refresh_token") or "")
    if refresh_token != "":
        from google.auth.exceptions import RefreshError
        from google.auth.exceptions import GoogleAuthError
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        credentials = Credentials(
            token=None,
            refresh_token=refresh_token,
            token_uri="https://oauth2.googleapis.com/token",
            client_id=client_id,
            client_secret=client_secret,
            scopes=[GOOGLE_SCOPE],
        )
        try:
            credentials.refresh(Request())
            if credentials.refresh_token != refresh_token:
                _save_cache(env_file, {
                    "client_id": client_id,
                    "provider": "google",
                    "refresh_token": credentials.refresh_token,
                    "user": user,
                })
            return credentials.token
        except RefreshError as e:
            if not interactive:
                raise OAuthError(
                    "Google authorization expired; run or deploy the app from a terminal") from e
            print("Google authorization has expired. PDT will reconnect the account.")
        except GoogleAuthError as e:
            raise OAuthError(f"Google token refresh failed: {e}")
    if not interactive:
        raise OAuthError(
            "Google authorization is required; run or deploy the app from a terminal")
    _confirm("google", user)
    access_token, refresh_token = _google_authorize(
        user, client_id, client_secret)
    _save_cache(env_file, {
        "client_id": client_id,
        "provider": "google",
        "refresh_token": refresh_token,
        "user": user,
    })
    print(f"Connected Google Gmail for {user}.")
    return access_token


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
            raise OAuthError(
                f"Microsoft OAuth cache is invalid; remove {CACHE_ENV} and try again") from e
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
        result = app.acquire_token_silent([MICROSOFT_SCOPE], account=accounts[0])
    if result and "access_token" in result:
        new_refresh_tokens = list(cache.search(
            msal.TokenCache.CredentialType.REFRESH_TOKEN))
        if new_refresh_tokens != old_refresh_tokens:
            _save_cache(env_file, {
                "client_id": client_id,
                "msal": cache.serialize(),
                "provider": "microsoft",
                "user": user,
            })
        return result["access_token"]
    if not interactive:
        raise OAuthError(
            "Microsoft authorization is required; run or deploy the app from a terminal")
    _confirm("microsoft", user)
    try:
        result = app.acquire_token_interactive(
            [MICROSOFT_SCOPE],
            login_hint=user,
            prompt="select_account",
        )
    except Exception as e:
        raise OAuthError(f"Microsoft authorization failed: {e}")
    if "access_token" not in result:
        detail = result.get("error_description") or result.get("error") or "unknown error"
        raise OAuthError(f"Microsoft authorization failed: {detail}")
    _save_cache(env_file, {
        "client_id": client_id,
        "msal": cache.serialize(),
        "provider": "microsoft",
        "user": user,
    })
    account = (result.get("id_token_claims") or {}).get("preferred_username") or user
    print(f"Connected Microsoft for {account}.")
    return result["access_token"]


def get_access_token(host: str, user: str, client_id: str,
                     client_secret: str, tenant_id: str, cache_b64: str,
                     env_file: Path | None = None,
                     interactive: bool | None = None) -> str:
    provider = provider_for_host(host)
    problems = oauth_config_errors(
        host, client_id, client_secret, cache_b64)
    if problems:
        raise OAuthError("; ".join(problems))
    key = (provider, user.casefold(), client_id)
    if key in _ACCESS_TOKENS:
        return _ACCESS_TOKENS[key]
    cache = _decode_cache(cache_b64, provider, user, client_id)
    interactive_ok = can_prompt(interactive)
    if provider == "google":
        token = _google_access_token(
            user, client_id, client_secret, cache, env_file, interactive_ok)
    else:
        token = _microsoft_access_token(
            user, client_id, tenant_id or "common", cache, env_file, interactive_ok)
    _ACCESS_TOKENS[key] = token
    return token
