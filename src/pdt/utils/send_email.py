"""Send a plain-text email via exactly one configured transport.

Used as a library (`from pdt.utils.send_email import send_email`) or CLI.

Env — set exactly one block:
  PDT_SMTP_HOST + PDT_SMTP_USER + password or OAuth configuration
  PDT_SES_REGION, optionally with PDT_SES_ACCESS_KEY_ID + PDT_SES_SECRET_ACCESS_KEY
  PDT_RESEND_API_KEY
  PDT_GRAPH_MAIL_USER + PDT_GRAPH_MAIL_CLIENT_ID (delegated Mail.Send)
If none are set, the body is printed to stdout instead of sent.

CLI:
  python -m pdt.utils.send_email --from a@x --to b@y --subject "hi" <<< "body"
  PDT_EMAIL_FROM / PDT_EMAIL_TO are used if --from / --to are omitted.

Exit codes: 0 ok, 1 bad config, 3 send failure.
"""

from __future__ import annotations

import argparse
import json
import os
import smtplib
import ssl
import sys
from email.message import EmailMessage
from pathlib import Path

from pdt import config
from pdt.utils import email_auth
from pdt.utils import send_email_graph
from pdt.utils.web import http_json
from pdt.utils.log import die, log

EXIT_OK = 0
EXIT_CONFIG = 1
EXIT_EMAIL = 3

SMTP_REQUIRED = (
    "PDT_SMTP_HOST",
    "PDT_SMTP_USER",
)
SMTP_VARS = (
    *SMTP_REQUIRED,
    "PDT_SMTP_PORT",
    "PDT_SMTP_PASSWORD",
    "PDT_SMTP_AUTH",
    "PDT_SMTP_OAUTH_CLIENT_ID",
    "PDT_SMTP_OAUTH_CLIENT_SECRET",
    "PDT_SMTP_OAUTH_TENANT_ID",
    email_auth.CACHE_ENV,
)


def optional_env(name: str) -> str:
    return os.environ.get(name, "").strip()


def require_env(name: str) -> str:
    val = optional_env(name)
    if val == "":
        die(EXIT_CONFIG, "missing env var", name=name)
    return val


def split_addrs(raw) -> list:
    if isinstance(raw, list):
        parts = raw
    else:
        parts = str(raw).split(",")
    addrs = []
    for part in parts:
        addr = str(part).strip()
        if addr != "":
            addrs.append(addr)
    if len(addrs) == 0:
        die(EXIT_CONFIG, "email to-address is empty")
    return addrs


def _smtp_auth_method() -> str:
    try:
        return email_auth.smtp_auth_method(
            optional_env("PDT_SMTP_HOST"),
            optional_env("PDT_SMTP_PASSWORD"),
            optional_env("PDT_SMTP_AUTH"),
        )
    except email_auth.OAuthError as e:
        die(EXIT_CONFIG, "invalid smtp authentication configuration", error=str(e))


def email_config_problems(check_authorization: bool = False,
                          check_oauth: bool = True) -> list[str]:
    """Return email configuration problems without changing authorization state.

    check_oauth is False for a caller that runs the interactive OAuth setup
    itself, so the setup is not blocked by the values it collects.
    """
    ses_key_vars = (
        "PDT_SES_ACCESS_KEY_ID",
        "PDT_SES_SECRET_ACCESS_KEY",
    )
    ses_vars = ("PDT_SES_REGION", *ses_key_vars)
    configured = []
    problems = []
    if any(optional_env(name) != "" for name in SMTP_VARS):
        missing = [name for name in SMTP_REQUIRED if optional_env(name) == ""]
        problems.extend(f"missing required env var {name}" for name in missing)
        configured.append("smtp")
    if any(optional_env(name) != "" for name in ses_vars):
        missing = []
        if optional_env("PDT_SES_REGION") == "":
            missing.append("PDT_SES_REGION")
        supplied_keys = [name for name in ses_key_vars if optional_env(name) != ""]
        if len(supplied_keys) == 1:
            missing.extend(name for name in ses_key_vars if optional_env(name) == "")
        problems.extend(f"missing required env var {name}" for name in missing)
        configured.append("ses")
    if optional_env("PDT_RESEND_API_KEY") != "":
        configured.append("resend")
    if any(optional_env(name) != "" for name in send_email_graph.VARS):
        problems.extend(send_email_graph.config_problems(
            check_oauth=check_oauth,
            check_authorization=check_authorization,
        ))
        configured.append("graph")
    if len(configured) > 1:
        problems.append(
            f"set only one email option; found {', '.join(configured)}")
    if configured == ["smtp"] and not any(
            problem.startswith("missing required") for problem in problems):
        try:
            method = email_auth.smtp_auth_method(
                optional_env("PDT_SMTP_HOST"),
                optional_env("PDT_SMTP_PASSWORD"),
                optional_env("PDT_SMTP_AUTH"),
            )
        except email_auth.OAuthError as e:
            problems.append(str(e))
        else:
            if method == "oauth2" and check_oauth:
                problems.extend(email_auth.oauth_config_errors(
                    optional_env("PDT_SMTP_HOST"),
                    optional_env("PDT_SMTP_OAUTH_CLIENT_ID"),
                    optional_env("PDT_SMTP_OAUTH_CLIENT_SECRET"),
                    optional_env(email_auth.CACHE_ENV),
                    check_authorization=check_authorization,
                ))
    return problems


def _oauth_client_missing() -> bool:
    """True when SMTP OAuth applies but the client ID or secret is empty."""
    host = optional_env("PDT_SMTP_HOST")
    if optional_env("PDT_SMTP_USER") == "" or email_auth.provider_for_host(host) is None:
        return False
    try:
        method = email_auth.smtp_auth_method(
            host, optional_env("PDT_SMTP_PASSWORD"), optional_env("PDT_SMTP_AUTH"))
    except email_auth.OAuthError:
        return False
    if method != "oauth2":
        return False
    if optional_env("PDT_SMTP_OAUTH_CLIENT_ID") == "":
        return True
    return (email_auth.provider_for_host(host) == "google"
            and optional_env("PDT_SMTP_OAUTH_CLIENT_SECRET") == "")


def pick_transport(env_file: Path | None = None) -> str:
    """Select and validate exactly one configured email transport."""
    problems = email_config_problems()
    if problems:
        if send_email_graph.client_missing():
            for line in send_email_graph.setup_help(env_file):
                log("error", line)
        elif _oauth_client_missing():
            for line in email_auth.oauth_setup_help(
                    optional_env("PDT_SMTP_HOST"), optional_env("PDT_SMTP_AUTH"), env_file):
                log("error", line)
        die(EXIT_CONFIG, "invalid email configuration",
            problems="; ".join(problems))
    configured = _configured()
    if len(configured) == 0:
        return "stdout"
    return configured[0]


def _configured() -> list[str]:
    configured = []
    if any(optional_env(name) != "" for name in SMTP_VARS):
        configured.append("smtp")
    if optional_env("PDT_SES_REGION") != "":
        configured.append("ses")
    if optional_env("PDT_RESEND_API_KEY") != "":
        configured.append("resend")
    if any(optional_env(name) != "" for name in send_email_graph.VARS):
        configured.append("graph")
    return configured


def email_problems(cfg: dict, check_oauth: bool = True) -> list[str]:
    """Env and config.yml problems for an app that sends email."""
    problems = email_config_problems(check_authorization=True,
                                     check_oauth=check_oauth)
    email_from = str(cfg.get("email_from") or "").strip()
    if _configured() and email_from == "":
        problems.append("email_from is required when sending email; set it in config.yml")
    mismatch = send_email_graph.sender_mismatch(email_from)
    if mismatch is not None:
        problems.append(mismatch)
    return problems


def _oauth_access_token(env_file: Path | None = None,
                        interactive: bool | None = None) -> str:
    try:
        return email_auth.get_access_token(
            optional_env("PDT_SMTP_HOST"),
            optional_env("PDT_SMTP_USER"),
            optional_env("PDT_SMTP_OAUTH_CLIENT_ID"),
            optional_env("PDT_SMTP_OAUTH_CLIENT_SECRET"),
            optional_env("PDT_SMTP_OAUTH_TENANT_ID"),
            optional_env(email_auth.CACHE_ENV),
            env_file=env_file,
            interactive=interactive,
        )
    except email_auth.OAuthError as e:
        die(EXIT_CONFIG, "smtp oauth failed", error=str(e))


def prepare_email_auth(env_file: Path | None = None,
                       interactive: bool | None = None) -> None:
    """Complete configured OAuth before the app performs other work."""
    if env_file is not None and email_auth.can_prompt(interactive):
        if send_email_graph.client_missing():
            saved = send_email_graph.prompt_client(env_file)
            if not saved:
                die(EXIT_CONFIG, "graph oauth client setup was not completed",
                    env_file=str(env_file))
        elif _oauth_client_missing():
            saved = email_auth.prompt_oauth_client(
                optional_env("PDT_SMTP_HOST"),
                optional_env("PDT_SMTP_AUTH"),
                optional_env("PDT_SMTP_OAUTH_CLIENT_ID"),
                optional_env("PDT_SMTP_OAUTH_CLIENT_SECRET"),
                env_file,
            )
            if not saved:
                die(EXIT_CONFIG, "smtp oauth client setup was not completed",
                    env_file=str(env_file))
    transport = pick_transport(env_file)
    if transport == "graph":
        send_email_graph.prepare(env_file, interactive)
        return
    if transport != "smtp" or _smtp_auth_method() != "oauth2":
        return
    _oauth_access_token(env_file, interactive)


# Microsoft 365 replies "535 5.7.139 Authentication unsuccessful, <reason>"; keyed by a substring of <reason>.
_M365_FIXES = (
    ("disabled for the Mailbox", (
        "SMTP AUTH is disabled for this mailbox.",
        "Fix: Microsoft 365 admin center > Users > Active users > <user> > Mail > Manage email apps > check Authenticated SMTP.",
        "Or PowerShell: Set-CASMailbox -Identity <user> -SmtpClientAuthenticationDisabled $false",
    )),
    ("disabled for the Tenant", (
        "SMTP AUTH is disabled for the whole tenant.",
        "Fix: Exchange admin center > Settings > Mail flow > uncheck 'Turn off SMTP AUTH protocol for your organization'.",
        "Or PowerShell: Set-TransportConfig -SmtpClientAuthenticationDisabled $false",
    )),
    ("basic authentication is disabled", (
        "Basic authentication is turned off for this tenant, so SMTP AUTH with a password cannot work.",
        "Fix: set PDT_SMTP_AUTH=oauth2 and authorize the Microsoft account.",
    )),
    ("security defaults", (
        "Security defaults or a Conditional Access policy blocks legacy (password) authentication.",
        "Fix: set PDT_SMTP_AUTH=oauth2 and authorize the Microsoft account.",
    )),
    ("credentials were incorrect", (
        "Microsoft rejected the username or password.",
        "Fix: check the SMTP credentials, or set PDT_SMTP_AUTH=oauth2.",
    )),
)


def _m365_fix(text: str) -> tuple:
    for needle, lines in _M365_FIXES:
        if needle.lower() in text.lower():
            return lines
    return (
        "Microsoft 365 rejected SMTP AUTH for this account.",
        "Check that Authenticated SMTP is enabled for the mailbox.",
        "Use PDT_SMTP_AUTH=oauth2 if Microsoft rejects password authentication.",
    )


def _send_smtp(from_addr: str, to_addrs: list, subject: str, body: str,
               html: str = "") -> None:
    host = require_env("PDT_SMTP_HOST")
    user = require_env("PDT_SMTP_USER")
    password = optional_env("PDT_SMTP_PASSWORD")
    auth_method = _smtp_auth_method()
    access_token = ""
    if auth_method == "oauth2":
        access_token = _oauth_access_token()
    port_s = optional_env("PDT_SMTP_PORT") or "587"
    try:
        port = int(port_s)
    except ValueError:
        die(EXIT_CONFIG, "PDT_SMTP_PORT is not a number", value=port_s)

    msg = EmailMessage()
    msg["From"] = from_addr
    msg["To"] = ", ".join(to_addrs)
    msg["Subject"] = subject
    msg.set_content(body)
    if html != "":
        msg.add_alternative(html, subtype="html")

    try:
        if port == 465:
            with smtplib.SMTP_SSL(host, port, context=ssl.create_default_context(), timeout=30) as smtp:
                if auth_method == "password":
                    smtp.login(user, password)
                else:
                    # smtp.auth() sends no EHLO of its own, and smtplib reads the
                    # server's "503 send hello first" as success.
                    smtp.ehlo()
                    smtp.auth("XOAUTH2", lambda challenge=None:
                              email_auth.xoauth2_response(user, access_token))
                smtp.send_message(msg)
            return
        with smtplib.SMTP(host, port, timeout=30) as smtp:
            smtp.starttls(context=ssl.create_default_context())
            if auth_method == "password":
                smtp.login(user, password)
            else:
                smtp.ehlo()
                smtp.auth("XOAUTH2", lambda challenge=None:
                          email_auth.xoauth2_response(user, access_token))
            smtp.send_message(msg)
    except smtplib.SMTPAuthenticationError as e:
        text = e.smtp_error.decode(errors="replace") if isinstance(e.smtp_error, bytes) else str(e.smtp_error)
        if "5.7.139" in text:
            for line in _m365_fix(text):
                log("error", line)
            die(EXIT_EMAIL, "microsoft 365 rejected smtp login", host=host, user=user, error=text)
        die(EXIT_EMAIL, "smtp login failed", host=host, user=user, error=text)
    except smtplib.SMTPException as e:
        die(EXIT_EMAIL, "smtp send failed", error=str(e))
    except OSError as e:
        die(EXIT_EMAIL, "smtp connection failed", error=str(e))


def _send_ses(from_addr: str, to_addrs: list, subject: str, body: str,
              html: str = "") -> None:
    region = require_env("PDT_SES_REGION")
    access_key = optional_env("PDT_SES_ACCESS_KEY_ID")
    secret_key = optional_env("PDT_SES_SECRET_ACCESS_KEY")
    try:
        import boto3
        from botocore.exceptions import BotoCoreError, ClientError
        client_args = {"region_name": region}
        if access_key != "":
            client_args.update(
                aws_access_key_id=access_key,
                aws_secret_access_key=secret_key,
            )
        client = boto3.client("ses", **client_args)
        message_body = {"Text": {"Data": body}}
        if html != "":
            message_body["Html"] = {"Data": html}
        client.send_email(
            Source=from_addr,
            Destination={"ToAddresses": to_addrs},
            Message={
                "Subject": {"Data": subject},
                "Body": message_body,
            },
        )
    except (BotoCoreError, ClientError) as e:
        die(EXIT_EMAIL, "ses send failed", error=str(e))


def _send_resend(from_addr: str, to_addrs: list, subject: str, body: str,
                 html: str = "") -> None:
    api_key = require_env("PDT_RESEND_API_KEY")
    fields = {
        "from": from_addr,
        "to": to_addrs,
        "subject": subject,
        "text": body,
    }
    if html != "":
        fields["html"] = html
    payload = json.dumps(fields).encode()
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "User-Agent": "pdt/1.0",
    }
    http_json(
        "https://api.resend.com/emails",
        EXIT_EMAIL,
        method="POST",
        headers=headers,
        data=payload,
    )


def send_email(from_addr: str, to_addrs, subject: str, body: str, html: str = "") -> str:
    """Send body using the one env-configured transport. Returns that name.

    An html part is offered alongside body; the reader's client picks one.
    Microsoft Graph sends the html alone.
    """
    transport = pick_transport()
    if transport == "stdout":
        print(body, flush=True)
        return transport
    if from_addr.strip() == "":
        die(EXIT_CONFIG, "email from-address is empty")
    addrs = split_addrs(to_addrs)
    if transport == "smtp":
        _send_smtp(from_addr, addrs, subject, body, html)
        return transport
    if transport == "ses":
        _send_ses(from_addr, addrs, subject, body, html)
        return transport
    if transport == "resend":
        _send_resend(from_addr, addrs, subject, body, html)
        return transport
    if transport == "graph":
        send_email_graph.send_mail(from_addr, addrs, subject, body, html)
        return transport
    die(EXIT_CONFIG, "unknown email transport", transport=transport)


def main() -> int:
    env_files = config.load_env(Path.cwd())
    parser = argparse.ArgumentParser(
        description="Send a plain-text email (smtp / ses / resend / graph / stdout).")
    parser.add_argument("--from", dest="from_addr", default=optional_env("PDT_EMAIL_FROM"))
    parser.add_argument("--to", dest="to_addrs", default=optional_env("PDT_EMAIL_TO"))
    parser.add_argument("--subject", default="Notification")
    args = parser.parse_args()
    body = sys.stdin.read()
    env_file = env_files[0] if env_files else Path.cwd() / ".env"
    prepare_email_auth(env_file)
    transport = pick_transport()
    if transport != "stdout" and args.from_addr == "":
        die(EXIT_CONFIG, "need --from or PDT_EMAIL_FROM")
    if transport != "stdout" and args.to_addrs == "":
        die(EXIT_CONFIG, "need --to or PDT_EMAIL_TO")
    used = send_email(args.from_addr, args.to_addrs, args.subject, body)
    if used == "stdout":
        log("info", "email not configured; printed to stdout")
    else:
        log("info", "alert sent", transport=used, to=split_addrs(args.to_addrs))
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
