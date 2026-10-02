"""`pdt gui`: start the dashboard server and open it in the browser.

The server is `gui_server.py`, run through `uv run --script` like a
provider script, so Django installs into its own environment and never
into the user's. It listens on localhost only. `--background` leaves it
running after the command returns and records its pid in .pdt/gui.pid,
so `pdt gui --stop` can end it. Running `pdt gui` again while a server
is up only opens the browser.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

from pdt import config, console

DEFAULT_PORT = 8765
PID_FILE = "gui.pid"
LOG_FILE = "gui.log"
START_TIMEOUT = 300
POLL = 0.3


def state_dir(project: Path) -> Path:
    """The project's .pdt folder, created and self-ignoring like config.write_state."""
    folder = project / config.STATE_DIR
    folder.mkdir(exist_ok=True)
    (folder / ".gitignore").write_text("*\n")
    return folder


def url(port: int) -> str:
    return f"http://127.0.0.1:{port}/"


def is_up(port: int) -> bool:
    try:
        urllib.request.urlopen(url(port), timeout=1).close()
    except urllib.error.HTTPError:
        return True
    except OSError:
        return False
    return True


def wait_up(proc: subprocess.Popen, port: int, timeout: float | None = None) -> bool:
    """True once the server answers; False when it exits first or the timeout passes."""
    deadline = time.monotonic() + (START_TIMEOUT if timeout is None else timeout)
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            return False
        if is_up(port):
            return True
        time.sleep(POLL)
    return False


def server_command(port: int) -> list[str]:
    script = Path(__file__).with_name("gui_server.py")
    return ["uv", "run", "--script", str(script), "--port", str(port)]


def read_pid(state: Path) -> dict | None:
    path = state / PID_FILE
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text())
    except ValueError:
        return None


def open_browser(port: int) -> None:
    if webbrowser.open(url(port)):
        console.done(f"Opened {url(port)} in your browser.")
    else:
        console.field("Open this in your browser", url(port))


def start(port: int, background: bool, open_the_browser: bool) -> int:
    project = config.find_project()
    state = state_dir(project)
    known = read_pid(state)
    if known is not None and is_up(known["port"]):
        console.say(f"pdt gui is already running at {url(known['port'])}")
        if open_the_browser:
            open_browser(known["port"])
        return 0
    env = dict(os.environ, PDT_PROJECT=str(project))
    if background:
        log = open(state / LOG_FILE, "ab")
        detached = ({"creationflags": subprocess.DETACHED_PROCESS
                     | subprocess.CREATE_NEW_PROCESS_GROUP}
                    if os.name == "nt" else {"start_new_session": True})
        proc = subprocess.Popen(server_command(port), env=env, stdin=subprocess.DEVNULL,
                                stdout=log, stderr=subprocess.STDOUT, **detached)
    else:
        proc = subprocess.Popen(server_command(port), env=env)
    (state / PID_FILE).write_text(json.dumps({"pid": proc.pid, "port": port}))
    console.status(f"starting pdt gui on port {port} (the first start installs Django)...")
    if not wait_up(proc, port):
        (state / PID_FILE).unlink(missing_ok=True)
        if proc.poll() is None:
            proc.terminate()
        console.error("pdt gui did not start"
                      + (f"; see {state / LOG_FILE}" if background else ""))
        return 1
    if open_the_browser:
        open_browser(port)
    if background:
        console.done(f"pdt gui is running at {url(port)} (log: {state / LOG_FILE})")
        console.command("pdt gui --stop", "stop it")
        return 0
    console.say(f"pdt gui is running at {url(port)}. Press Ctrl+C to stop it.")
    try:
        return proc.wait()
    except KeyboardInterrupt:
        proc.terminate()
        proc.wait()
        return 0
    finally:
        (state / PID_FILE).unlink(missing_ok=True)


def stop() -> int:
    state = state_dir(config.find_project())
    known = read_pid(state)
    if known is None:
        console.say("pdt gui is not running.")
        return 0
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(known["pid"]), "/T", "/F"],
                       capture_output=True, check=False)
    else:
        try:
            os.kill(known["pid"], signal.SIGTERM)
        except ProcessLookupError:
            pass
    (state / PID_FILE).unlink(missing_ok=True)
    console.done("Stopped pdt gui.")
    return 0


def main(port: int, background: bool, no_browser: bool, stop_it: bool) -> int:
    if stop_it:
        return stop()
    return start(port, background, not no_browser)


if __name__ == "__main__":
    sys.exit(main(DEFAULT_PORT, False, False, False))
