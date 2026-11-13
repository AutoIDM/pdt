"""Who is using a web app.

Identity-Aware Proxy, the sign-in gate in front of a deployed web app, sets
X-Goog-Authenticated-User-Email on every request it forwards. `current_user`
reads only that header: a visitor can send any other header, such as
X-Forwarded-Email, so no other header names the visitor. A local `pdt run` has
no gate, so `current_user` returns "" there.

    from pdt.utils.auth import current_user
    email = current_user(self.headers)
"""

from __future__ import annotations

# The value is "accounts.google.com:<email>".
IAP_EMAIL_HEADER = "X-Goog-Authenticated-User-Email"


def current_user(headers) -> str:
    """The signed-in person's email, or "".

    `headers` is any mapping with a case-insensitive `get`, such as
    `http.server.BaseHTTPRequestHandler.headers`, or a plain dict whose
    keys match the header name as written here.
    """
    value = headers.get(IAP_EMAIL_HEADER) or headers.get(IAP_EMAIL_HEADER.lower())
    if not value:
        return ""
    text = str(value).strip()
    return text.split(":", 1)[1] if text.startswith("accounts.google.com:") else text
