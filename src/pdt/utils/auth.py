"""Who is using a web app.

The platform's sign-in gate in front of a web app sets a header with the
visitor's email on every request it forwards, and strips the same header from
what the visitor sent, so the app can trust it. Each platform names the header
its own way; `current_user` reads all of them. A local `pdt run` has no gate,
so `current_user` returns "" there.

    from pdt.utils.auth import current_user
    email = current_user(self.headers)
"""

from __future__ import annotations

# oauth2-proxy (Windows)
EMAIL_HEADER = "X-Forwarded-Email"
USER_HEADER = "X-Forwarded-User"
# Identity-Aware Proxy (Cloud Run): the value is "accounts.google.com:<email>".
IAP_EMAIL_HEADER = "X-Goog-Authenticated-User-Email"
# Built-in authentication (Azure Container Apps)
AZURE_NAME_HEADER = "X-MS-CLIENT-PRINCIPAL-NAME"

HEADERS = (EMAIL_HEADER, USER_HEADER, IAP_EMAIL_HEADER, AZURE_NAME_HEADER)


def current_user(headers) -> str:
    """The signed-in person's email, else the gate's user id, else "".

    `headers` is any mapping with a case-insensitive `get`, such as
    `http.server.BaseHTTPRequestHandler.headers`, or a plain dict whose
    keys match the header names as written here.
    """
    for name in HEADERS:
        value = headers.get(name) or headers.get(name.lower())
        if value:
            text = str(value).strip()
            # IAP prefixes the account namespace.
            return text.split(":", 1)[1] if text.startswith("accounts.google.com:") else text
    return ""
