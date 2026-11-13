#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["pdt-cli[apps]==PDT_VERSION"]
# ///
"""Sample web app. Use as a template to create your own.

It serves at the port in PDT_PORT. Identity-Aware Proxy signs each visitor in
and sends their email in the X-Goog-Authenticated-User-Email header. A local
`pdt run` has no sign-in, so the greeting says "visitor".
"""

from __future__ import annotations

import os
import signal
import sys
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from pdt.utils.log import log

# Identity-Aware Proxy sets it to "accounts.google.com:<email>".
EMAIL_HEADER = "X-Goog-Authenticated-User-Email"


def stop(_signum, _frame) -> None:
    raise KeyboardInterrupt


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path == "/healthz":
            return self.reply("ok")
        who = (self.headers.get(EMAIL_HEADER) or "").removeprefix("accounts.google.com:") or "visitor"
        log("info", "page served", path=self.path, user=who)
        self.reply(f"Hello, {who}!")

    def reply(self, text: str) -> None:
        body = text.encode()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args) -> None:
        pass


def main() -> int:
    port = int(os.environ["PDT_PORT"])
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    signal.signal(signal.SIGTERM, stop)
    log("info", "serving", port=port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    log("info", "stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
