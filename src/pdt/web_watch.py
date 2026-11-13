"""`pdt run` of a web app: wait until it answers, then open it in the browser.

The deployed app must listen on all interfaces (0.0.0.0) at PDT_PORT, because the
platform reaches the container from outside it. An app that listens on 127.0.0.1
works on this computer and then fails its deploy, so `pdt run` checks for that too:
when the app answers on 127.0.0.1 but not on this computer's network address, it
listens on the loopback address only. An app in a container that listens on
127.0.0.1 never answers through Docker's published port, so it shows as an app
that did not answer at all.
"""

from __future__ import annotations

import os
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser

from pdt import console
from pdt.profiles import web

# How long `pdt run` waits for the first answer: the same limit as a deployed app's startup check.
START_WAIT = 90.0
POLL = 0.5


def answers(url: str) -> bool:
    """Whether an HTTP server answered the URL, with any status."""
    try:
        with urllib.request.urlopen(url, timeout=2):
            return True
    except urllib.error.HTTPError:
        return True
    except (OSError, ValueError):
        return False


def network_address() -> str:
    """This computer's address on its network, or "" with no network. Connecting a UDP socket
    sends nothing; it only picks the interface."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        try:
            probe.connect(("192.0.2.1", 9))
            address = probe.getsockname()[0]
        except OSError:
            return ""
    return "" if address.startswith("127.") else address


def listens_on_all_interfaces(port: int) -> bool | None:
    """False when nothing accepts a connection on the network address, None with no network."""
    address = network_address()
    if address == "":
        return None
    try:
        with socket.create_connection((address, port), timeout=2):
            return True
    except OSError:
        return False


def can_open_browser() -> bool:
    return sys.stdout.isatty() and os.environ.get("CI", "").strip() == ""


def watch(app: dict, local_url: str, container: bool) -> threading.Thread:
    """Start a thread that reports when the app answers, and opens local_url once it does."""
    port = web.port(app)
    health = local_url.rstrip("/") + web.health_path(app)

    def run() -> None:
        deadline = time.monotonic() + START_WAIT
        while time.monotonic() < deadline:
            if answers(health):
                break
            time.sleep(POLL)
        else:
            console.warn(f"nothing answered {console.value(health)} within "
                         f"{console.value(f'{START_WAIT:.0f}')} s. "
                         f"The app must serve HTTP on port {console.value(str(port))} (the "
                         f"value of {console.value(web.ENV_PORT)}) on all interfaces, "
                         f"{console.value('0.0.0.0')}; a deploy fails the same way")
            return
        if not container and listens_on_all_interfaces(port) is False:
            console.warn(f"the app answers on {console.value('127.0.0.1')} only. A deployed app "
                         f"must listen on {console.value('0.0.0.0')}, or its deploy fails")
        console.done(f"{console.value(app['name'])} answers at {console.value(local_url)}")
        if can_open_browser():
            webbrowser.open(local_url)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread
