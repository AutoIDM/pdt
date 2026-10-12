"""`pdt gui`: start the dashboard server and open it in the browser.

The server is `gui_server.py`, run through `uv run --script` like a
provider script, so Django installs into its own environment and never
into the user's. It listens on localhost only. The terminal gets one
line, the address, and after it only what needs the user: uv runs quiet,
and the server logs to .pdt/gui.log. `--background` leaves it running
after the command returns and records its pid in .pdt/gui.pid, so
`pdt gui --stop` can end it. Running `pdt gui` again while a server
is up only opens the browser.
"""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

from pdt import config, console

PID_FILE = "gui.pid"
LOG_FILE = "gui.log"
# The log keeps its newest half when it grows past this.
LOG_LIMIT = 1024 * 1024
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


def is_up(port: int, project: Path) -> bool:
    """True when the pdt gui of `project` answers on the port, not another program."""
    try:
        with urllib.request.urlopen(url(port) + "pdt/", timeout=1) as answer:
            return answer.read().decode(errors="replace") == str(project)
    except OSError:
        return False


def port_free(port: int) -> bool:
    with socket.socket() as probe:
        # Elsewhere a stopped server's closed connections would block the port for a
        # minute without it; on Windows it would let the probe bind over a live server.
        if os.name != "nt":
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def wait_up(proc: subprocess.Popen, port: int, project: Path,
            timeout: float | None = None) -> bool:
    """True once the server answers; False when it exits first or the timeout passes."""
    deadline = time.monotonic() + (START_TIMEOUT if timeout is None else timeout)
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            return False
        if is_up(port, project):
            return True
        time.sleep(POLL)
    return False


def server_command(port: int) -> list[str]:
    script = Path(__file__).with_name("gui_server.py")
    return ["uv", "run", "--quiet", "--script", str(script), "--port", str(port)]


def read_pid(state: Path) -> dict | None:
    path = state / PID_FILE
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text())
    except ValueError:
        return None


def open_browser(port: int) -> None:
    if not webbrowser.open(url(port)):
        console.field("Open this in your browser", url(port))


def start(port: int, background: bool, open_the_browser: bool) -> int:
    project = config.find_project()
    state = state_dir(project)
    known = read_pid(state)
    if known is not None and is_up(known["port"], project):
        console.say(f"pdt gui is already running at {console.value(url(known['port']))}")
        if open_the_browser:
            open_browser(known["port"])
        return 0
    log_path = state / LOG_FILE
    if log_path.is_file() and log_path.stat().st_size > LOG_LIMIT:
        log_path.write_bytes(log_path.read_bytes()[-LOG_LIMIT // 2:])
    if not port_free(port):
        console.error(f"another program uses port {console.value(port)}. Run "
                      f"{console.value(f'pdt gui --port {port + 1}')} to use another port.")
        return 1
    env = dict(os.environ, PDT_PROJECT=str(project))
    if background:
        log = open(log_path, "ab")
        detached = ({"creationflags": subprocess.DETACHED_PROCESS
                     | subprocess.CREATE_NEW_PROCESS_GROUP}
                    if os.name == "nt" else {"start_new_session": True})
        proc = subprocess.Popen(server_command(port), env=env, stdin=subprocess.DEVNULL,
                                stdout=log, stderr=subprocess.STDOUT, **detached)
    else:
        proc = subprocess.Popen(server_command(port), env=env)
    (state / PID_FILE).write_text(json.dumps({"pid": proc.pid, "port": port}))
    if not background:
        console.say(f"pdt gui runs at {console.value(url(port))}. Press Ctrl+C to stop it.")
    if not wait_up(proc, port, project):
        (state / PID_FILE).unlink(missing_ok=True)
        if proc.poll() is None:
            proc.terminate()
        console.error(f"pdt gui did not start; see {console.value(state / LOG_FILE)}")
        return 1
    if open_the_browser:
        open_browser(port)
    if background:
        console.done(f"pdt gui is running at {console.value(url(port))} "
                     f"(log: {console.value(state / LOG_FILE)})")
        console.command("pdt gui --stop", "stop it")
        return 0
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
    sys.exit(main(config.GUI_PORT, False, False, False))
