"""The email alerts type: one email per event, sent with the method the entry names.

An entry is `{type: email, method: <method>, from: <address>, to: [<address>, ...]}`
plus its events. `METHODS` holds, for each method, the env vars it needs and the
ones it reads when they are set. The values are secrets, so they live in `.env`,
and `config.env_spec` adds the names to every app's env spec, so `pdt deploy`
sends them in the app's cloud secret. PDT_EMAIL_PRIVATE_KEY_PATH names a .pfx
file with no password; deploy sends it as PDT_EMAIL_PRIVATE_KEY_B64, as it does
for every `_PATH` variable.

The event's `title` is the subject, its `text` the plain body, and its `html`
the HTML body. An event with neither body gets both from its Message (`plain`
and `page`), and one with only `html` gets its plain body from `plain`. A `to`
parameter of the event (a list of addresses, or one string with commas) replaces
the entry's `to` for that event. Microsoft Graph takes one body, so
`microsoft-365` sends the HTML body when the event has one.
`azure-communication-services` signs its request with the access key of
PDT_ACS_CONNECTION_STRING (`endpoint=...;accesskey=...`) with the standard
library alone.

This module is in deploy_common.LIFECYCLE_MODULES, so it imports only what
lifecycle.py's script header installs.
The file is not named email.py, because lifecycle.py runs as a script from this
folder, where that name would hide the standard library's `email` package.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import smtplib
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from email.message import EmailMessage
from email.utils import formatdate
from html import escape
from pathlib import Path

from pdt import alert_message, config
from pdt.alert_message import Message

KEYS = frozenset({"method", "from", "to"})
METHODS = {
    "azure-communication-services": (("PDT_ACS_CONNECTION_STRING",), ()),
    "microsoft-365": (("PDT_EMAIL_TENANT_ID", "PDT_EMAIL_CLIENT_ID",
                       "PDT_EMAIL_PRIVATE_KEY_PATH"), ("PDT_EMAIL_PRIVATE_KEY_B64",)),
    "smtp": (("PDT_SMTP_HOST", "PDT_SMTP_USER", "PDT_SMTP_PASSWORD"), ("PDT_SMTP_PORT",)),
}
GRAPH_SCOPE = "https://graph.microsoft.com/.default"
CERT_JWT_SECONDS = 60 * 10
ACS_API_VERSION = "2023-03-31"
# The sending limits of a subscription, per minute and per hour: an Azure managed domain
# (azurecomm.net), and a custom domain.
ACS_LIMITS = {"azure": (5, 10), "custom": (30, 100)}


def problem(entry: dict) -> str:
    """What is wrong with the entry's method, from, or to, or ""."""
    if entry.get("method") not in METHODS:
        return f"needs method, one of: {', '.join(METHODS)}"
    if not isinstance(entry.get("from"), str) or entry["from"].strip() == "":
        return "needs from, the address the email comes from"
    to = entry.get("to")
    if not (isinstance(to, list) and to and all(isinstance(a, str) and a.strip() for a in to)):
        return "needs to, a list of addresses, for example to: [it-ops@example.com]"
    return ""


def secrets(entry: dict) -> list[str]:
    return list(METHODS[entry["method"]][0])


def optional(entry: dict) -> list[str]:
    return list(METHODS[entry["method"]][1])


def label(entry: dict) -> str:
    return ", ".join(entry["to"])


def addresses(raw: object) -> list[str]:
    parts = raw if isinstance(raw, list) else str(raw or "").split(",")
    return [str(part).strip() for part in parts if str(part).strip()]


def unmarked(text: str) -> str:
    return re.sub(r"\*([^*]+)\*", r"\1", text).replace("`", "")


def marked(text: str) -> str:
    text = re.sub(r"\*([^*]+)\*", r"<b>\1</b>", escape(text))
    return re.sub(r"`([^`]+)`", r"<code>\1</code>", text).replace("\n", "<br>")


def plain(message: Message) -> str:
    lines = [message.title]
    if message.note:
        lines += ["", unmarked(message.note)]
    if message.text:
        lines += ["", unmarked(message.text)]
    if message.fields:
        lines += ["", *(f"{name}: {unmarked(value)}" for name, value in message.fields)]
    if message.url:
        lines += ["", f"Open: {message.url}"]
    if message.footer:
        lines += ["", unmarked(message.footer)]
    return "\n".join(lines) + "\n"


def page(message: Message) -> str:
    parts = [f'<h2 style="color:{message.tone.colour}">{escape(message.title)}</h2>']
    if message.note:
        parts.append(f"<p>{marked(message.note)}</p>")
    if message.text:
        parts.append(f"<p>{marked(message.text)}</p>")
    if message.fields:
        rows = "".join(f'<tr><td style="padding-right:16px"><b>{escape(name)}</b></td>'
                       f"<td>{marked(value)}</td></tr>" for name, value in message.fields)
        parts.append(f"<table>{rows}</table>")
    if message.url:
        parts.append(f'<p><a href="{escape(message.url)}">Open</a></p>')
    if message.footer:
        parts.append(f'<p style="color:#868686;font-size:small">{marked(message.footer)}</p>')
    return "<html><body>" + "".join(parts) + "</body></html>"


def send_smtp(values: dict, sender: str, to: list[str], subject: str, text: str,
              html: str) -> None:
    host = values["PDT_SMTP_HOST"]
    try:
        port = int(values["PDT_SMTP_PORT"] or 587)
    except ValueError:
        raise RuntimeError(f"PDT_SMTP_PORT is {values['PDT_SMTP_PORT']!r}, not a number") from None
    message = EmailMessage()
    message["From"] = sender
    message["To"] = ", ".join(to)
    message["Subject"] = subject
    message.set_content(text)
    if html != "":
        message.add_alternative(html, subtype="html")
    try:
        if port == 465:
            smtp = smtplib.SMTP_SSL(host, port, context=ssl.create_default_context(),
                                    timeout=alert_message.TIMEOUT_SECONDS)
        else:
            smtp = smtplib.SMTP(host, port, timeout=alert_message.TIMEOUT_SECONDS)
        with smtp:
            if port != 465:
                smtp.starttls(context=ssl.create_default_context())
            smtp.login(values["PDT_SMTP_USER"], values["PDT_SMTP_PASSWORD"])
            smtp.send_message(message)
    except (smtplib.SMTPException, OSError) as e:
        raise RuntimeError(f"the SMTP server {host} did not take the email: {e}") from None


def private_key(values: dict) -> bytes:
    if values["PDT_EMAIL_PRIVATE_KEY_B64"] != "":
        try:
            return base64.b64decode(values["PDT_EMAIL_PRIVATE_KEY_B64"], validate=True)
        except ValueError:
            raise RuntimeError("PDT_EMAIL_PRIVATE_KEY_B64 is not valid base64") from None
    path = Path(values["PDT_EMAIL_PRIVATE_KEY_PATH"]).expanduser()
    if not path.is_absolute():
        try:
            path = config.find_project() / path
        except config.ConfigError:
            path = Path.cwd() / path
    if not path.is_file():
        raise RuntimeError(f"PDT_EMAIL_PRIVATE_KEY_PATH names {path}, which is not a file")
    return path.read_bytes()


def graph_token(values: dict) -> str:
    """An app-only Microsoft Graph token, signed in with the certificate of the .pfx file."""
    try:
        import jwt
        from cryptography.hazmat.primitives.hashes import SHA256
        from cryptography.hazmat.primitives.serialization.pkcs12 import (
            load_key_and_certificates)
    except ImportError:
        raise RuntimeError("the microsoft-365 method needs the pyjwt and cryptography packages, "
                           "and they are not installed where this event was sent from") from None
    client_id = values["PDT_EMAIL_CLIENT_ID"]
    url = (f"https://login.microsoftonline.com/{values['PDT_EMAIL_TENANT_ID']}"
           "/oauth2/v2.0/token")
    try:
        key, cert, _extra = load_key_and_certificates(private_key(values), None)
    except ValueError as e:
        raise RuntimeError(f"the email certificate could not be loaded: {e}") from None
    if key is None or cert is None:
        raise RuntimeError("the email certificate file holds no private key or no certificate")
    now = int(time.time())
    assertion = jwt.encode(
        {"aud": url, "exp": now + CERT_JWT_SECONDS, "iss": client_id, "jti": str(uuid.uuid4()),
         "nbf": now, "sub": client_id}, key, algorithm="PS256",
        headers={"x5t#S256": base64.urlsafe_b64encode(
            cert.fingerprint(SHA256())).rstrip(b"=").decode("ascii")})
    form = {"scope": GRAPH_SCOPE, "client_id": client_id, "grant_type": "client_credentials",
            "client_assertion_type": "urn:ietf:params:oauth:client-assertion-type:jwt-bearer",
            "client_assertion": assertion}
    request = urllib.request.Request(url, data=urllib.parse.urlencode(form).encode(),
                                     method="POST")
    try:
        with urllib.request.urlopen(request, timeout=alert_message.TIMEOUT_SECONDS) as response:
            return str(json.loads(response.read())["access_token"])
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace").strip()
        raise RuntimeError(f"Microsoft sign-in answered HTTP {e.code}: {body or e.reason}") from None
    except urllib.error.URLError as e:
        raise RuntimeError(f"Microsoft sign-in could not be reached: {e.reason}") from None
    except (ValueError, KeyError):
        raise RuntimeError("Microsoft sign-in gave no access token") from None


def send_microsoft_365(values: dict, sender: str, to: list[str], subject: str, text: str,
                       html: str) -> None:
    body = {"contentType": "HTML", "content": html} if html != "" else {
        "contentType": "Text", "content": text}
    try:
        alert_message.post(
            f"https://graph.microsoft.com/v1.0/users/{urllib.parse.quote(sender)}/sendMail",
            {"message": {"subject": subject, "body": body, "toRecipients": [
                {"emailAddress": {"address": address}} for address in to]}},
            "Microsoft Graph", {"Authorization": f"Bearer {graph_token(values)}"})
    except RuntimeError as e:
        if '"ErrorInvalidUser"' not in str(e):
            raise
        raise RuntimeError(f"{sender} is not a mailbox in the Microsoft 365 organization of the "
                           f"app registration. {e}") from None


def acs_headers(url: str, body: bytes, access_key: str, date: str) -> dict:
    """The headers that sign a POST of `body` to `url` with the access key of a Communication
    Service (HMAC-SHA256 of the verb, path and query, date, host, and body hash)."""
    parts = urllib.parse.urlsplit(url)
    content_hash = base64.b64encode(hashlib.sha256(body).digest()).decode()
    signed = f"POST\n{parts.path}?{parts.query}\n{date};{parts.netloc};{content_hash}"
    try:
        key = base64.b64decode(access_key, validate=True)
    except ValueError:
        raise RuntimeError("the accesskey of PDT_ACS_CONNECTION_STRING is not valid base64") from None
    signature = base64.b64encode(hmac.digest(key, signed.encode(), hashlib.sha256)).decode()
    return {"x-ms-date": date, "x-ms-content-sha256": content_hash,
            "Authorization": "HMAC-SHA256 SignedHeaders=x-ms-date;host;x-ms-content-sha256"
                             f"&Signature={signature}"}


def send_azure_communication_services(values: dict, sender: str, to: list[str], subject: str,
                                      text: str, html: str) -> None:
    """Hand the email to Azure Communication Services. The service answers when it accepted
    the email, and sends it after that."""
    parts = {}
    for part in values["PDT_ACS_CONNECTION_STRING"].split(";"):
        name, _, value = part.partition("=")
        parts[name.strip().lower()] = value.strip()
    if not parts.get("endpoint") or not parts.get("accesskey"):
        raise RuntimeError("PDT_ACS_CONNECTION_STRING needs endpoint=...;accesskey=...")
    url = f"{parts['endpoint'].rstrip('/')}/emails:send?api-version={ACS_API_VERSION}"
    content = {"subject": subject, "plainText": text}
    if html != "":
        content["html"] = html
    payload = {"senderAddress": sender, "content": content,
               "recipients": {"to": [{"address": address} for address in to]}}
    headers = acs_headers(url, json.dumps(payload).encode(), parts["accesskey"],
                          formatdate(usegmt=True))
    try:
        alert_message.post(url, payload, "Azure Communication Services", headers)
    except RuntimeError as e:
        if "answered HTTP 429" not in str(e):
            raise
        domain = "azure" if sender.lower().endswith(".azurecomm.net") else "custom"
        minute, hour = ACS_LIMITS[domain]
        raise RuntimeError(f"Azure Communication Services refused the email: the subscription "
                           f"reached its limit of {minute} email messages each minute and "
                           f"{hour} each hour for {sender.rpartition('@')[2]}") from None


SENDERS = {"azure-communication-services": send_azure_communication_services,
           "microsoft-365": send_microsoft_365, "smtp": send_smtp}


def send(entry: dict, to: list[str], subject: str, text: str, html: str = "") -> None:
    needed, read = METHODS[entry["method"]]
    values = {name: alert_message.secret_value(name) for name in (*needed, *read)}
    missing = [name for name in needed if not config.is_set(values, name)]
    if missing:
        raise RuntimeError(f"{', '.join(missing)} is not set; run pdt alerts setup to connect "
                           "email")
    SENDERS[entry["method"]](values, str(entry["from"]), to, " ".join(subject.split()), text,
                             html)


def send_ready(entry: dict, project: str) -> None:
    message = alert_message.ready_message(project, label(entry), entry.get("events", []))
    send(entry, entry["to"], message.title, plain(message), page(message))


def deliver(event, entry: dict, unhandled: bool) -> None:
    message = alert_message.event_message(event, unhandled)
    params = event.params
    text, html = str(params.get("text") or ""), str(params.get("html") or "")
    if text == "" and html == "":
        html = page(message)
    if text == "":
        text = plain(message)
    send(entry, addresses(params.get("to")) or entry["to"],
         str(params.get("title") or message.summary), text, html)
