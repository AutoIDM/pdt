#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["pdt-cli[apps]==PDT_VERSION"]
# ///
"""Sample web app. Use as a template to create your own.

It serves at the port in PDT_PORT. The platform's sign-in in front of it
forwards each request with the visitor's email in a header;
`pdt.utils.auth.current_user` reads it. A local `pdt run` has no sign-in, so
the greeting says "visitor".

Exit codes: 0 ok, 1 bad config.
"""

from __future__ import annotations

import os
import signal
import sys
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from pdt.config import ConfigError, load_env, merged_app, missing_env
from pdt.utils.auth import current_user
from pdt.utils.log import die, log

EXIT_OK = 0
EXIT_CONFIG = 1


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path == "/healthz":
            return self.reply("ok")
        who = current_user(self.headers) or "visitor"
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
    app_dir = Path(__file__).resolve().parent
    try:
        app = merged_app(app_dir.name)
    except ConfigError as exc:
        die(EXIT_CONFIG, "config is not valid", problem=str(exc))
    load_env(app_dir)
    missing = missing_env(app)
    if missing != "":
        die(EXIT_CONFIG, f"env vars missing: {missing}")

    port = int(os.environ["PDT_PORT"])
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    signal.signal(signal.SIGTERM, lambda *_: server.shutdown())
    log("info", "serving", app=app["name"], port=port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    log("info", "stopped", app=app["name"])
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
