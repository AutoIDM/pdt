"""`pdt run` of a web app waits for the first answer, warns about a loopback-only server, and
opens the browser."""

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from pdt import web_watch


class Ok(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()

    def log_message(self, *args):
        pass


def app(port):
    return {"name": "dashboard", "web": {"port": port, "health_path": "/"}}


def serve():
    server = ThreadingHTTPServer(("127.0.0.1", 0), Ok)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def said(monkeypatch):
    """What the watcher thread prints: rich prints from a thread past pytest's capture."""
    lines = []
    for name in ("warn", "done"):
        monkeypatch.setattr(web_watch.console, name, lambda m: lines.append(m))
    return lines


def test_a_loopback_server_is_reported_and_the_browser_opens(monkeypatch):
    server = serve()
    port = server.server_address[1]
    opened = []
    lines = said(monkeypatch)
    monkeypatch.setattr(web_watch, "can_open_browser", lambda: True)
    monkeypatch.setattr(web_watch.webbrowser, "open", opened.append)
    monkeypatch.setattr(web_watch, "listens_on_all_interfaces", lambda port: False)
    url = f"http://127.0.0.1:{port}/"
    web_watch.watch(app(port), url, container=False).join(10)
    server.shutdown()
    out = " ".join(" ".join(lines).replace("[bold not dim]", "").replace("[/]", "").split())
    assert "listens on 127.0.0.1 only" in out and f"dashboard answers at {url}" in out
    assert opened == [url]


def test_a_server_that_never_answers_is_reported(monkeypatch):
    monkeypatch.setattr(web_watch, "START_WAIT", 0.3)
    lines = said(monkeypatch)
    monkeypatch.setattr(web_watch, "can_open_browser", lambda: True)
    monkeypatch.setattr(web_watch.webbrowser, "open", lambda url: (_ for _ in ()).throw(AssertionError))
    web_watch.watch(app(9), "http://127.0.0.1:9/", container=True).join(10)
    out = " ".join(" ".join(lines).replace("[bold not dim]", "").replace("[/]", "").split())
    assert "nothing answered http://127.0.0.1:9/" in out and "0.0.0.0" in out
