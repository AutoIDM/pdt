import http.server
import json
import socket
import subprocess
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from pdt import gui_cli


class FakeProc:
    def __init__(self, exits=False):
        self.pid = 4242
        self.exits = exits
        self.terminated = False

    def poll(self):
        return 1 if self.exits else None

    def wait(self):
        return 0

    def terminate(self):
        self.terminated = True


REAL_IS_UP = gui_cli.is_up


@pytest.fixture
def launcher(project, monkeypatch):
    calls = SimpleNamespace(popen=[], opened=[], killed=[], up=True)

    def fake_popen(command, **kwargs):
        calls.popen.append((command, kwargs))
        return FakeProc()

    monkeypatch.setattr(gui_cli.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(gui_cli, "is_up", lambda port, project: calls.up)
    monkeypatch.setattr(gui_cli.webbrowser, "open", lambda url: calls.opened.append(url) or True)
    monkeypatch.setattr(gui_cli.os, "kill", lambda pid, sig: calls.killed.append(pid))
    monkeypatch.setattr(gui_cli.subprocess, "run", lambda command, **kwargs: calls.killed.append(int(command[2])))
    monkeypatch.setattr(gui_cli, "POLL", 0)
    return calls


def test_the_server_command_runs_the_script_with_uv(project):
    command = gui_cli.server_command(9000)
    assert command[:4] == ["uv", "run", "--quiet", "--script"]
    assert command[4].endswith("gui_server.py")
    assert command[5:] == ["--port", "9000"]


def test_wait_up_gives_up_when_the_server_exits(monkeypatch):
    monkeypatch.setattr(gui_cli, "is_up", lambda port, project: False)
    assert gui_cli.wait_up(FakeProc(exits=True), 8765, Path("/p"), timeout=5) is False


def test_foreground_start_opens_the_browser_and_cleans_up(project, launcher, capsys):
    assert gui_cli.main(8765, False, False, False) == 0
    assert capsys.readouterr().out == (
        "pdt gui runs at http://127.0.0.1:8765/. Press Ctrl+C to stop it.\n")
    command, kwargs = launcher.popen[0]
    assert command == gui_cli.server_command(8765)
    assert kwargs["env"]["PDT_PROJECT"] == str(project)
    assert launcher.opened == ["http://127.0.0.1:8765/"]
    assert not (project / ".pdt" / "gui.pid").exists()
    assert (project / ".pdt" / ".gitignore").read_text() == "*\n"


def test_background_start_records_the_pid_and_says_how_to_stop(project, launcher, capsys):
    assert gui_cli.main(8765, True, True, False) == 0
    _command, kwargs = launcher.popen[0]
    assert kwargs["stdin"] is subprocess.DEVNULL
    assert json.loads((project / ".pdt" / "gui.pid").read_text()) == {"pid": 4242, "port": 8765}
    assert launcher.opened == []
    out = capsys.readouterr().out
    assert "pdt gui --stop" in out
    assert "gui.log" in out


def test_a_second_start_only_opens_the_browser(project, launcher, capsys):
    (project / ".pdt").mkdir()
    (project / ".pdt" / "gui.pid").write_text(json.dumps({"pid": 1, "port": 9001}))
    assert gui_cli.main(8765, False, False, False) == 0
    assert launcher.popen == []
    assert launcher.opened == ["http://127.0.0.1:9001/"]
    assert "already running" in capsys.readouterr().out


def test_a_start_that_never_answers_fails_and_ends_the_process(project, launcher, capsys,
                                                                monkeypatch):
    launcher.up = False
    monkeypatch.setattr(gui_cli, "START_TIMEOUT", 0.01)
    assert gui_cli.main(8765, False, False, False) == 1
    assert "did not start" in capsys.readouterr().out
    assert not (project / ".pdt" / "gui.pid").exists()


def test_stop_ends_the_recorded_process(project, launcher, capsys):
    (project / ".pdt").mkdir()
    (project / ".pdt" / "gui.pid").write_text(json.dumps({"pid": 77, "port": 8765}))
    assert gui_cli.main(8765, False, False, True) == 0
    assert launcher.killed == [77]
    assert not (project / ".pdt" / "gui.pid").exists()


def test_stop_with_nothing_running_says_so(project, launcher, capsys):
    assert gui_cli.main(8765, False, False, True) == 0
    assert "not running" in capsys.readouterr().out


def test_a_start_trims_a_long_log_to_its_newest_half(project, launcher, monkeypatch):
    monkeypatch.setattr(gui_cli, "LOG_LIMIT", 10)
    (project / ".pdt").mkdir()
    (project / ".pdt" / "gui.log").write_bytes(b"0123456789abcdef")
    assert gui_cli.main(8765, False, False, False) == 0
    assert (project / ".pdt" / "gui.log").read_bytes() == b"bcdef"


def test_a_busy_port_prints_one_plain_line_and_starts_nothing(project, launcher, capsys):
    with socket.socket() as taken:
        taken.bind(("127.0.0.1", 0))
        taken.listen()
        port = taken.getsockname()[1]
        assert gui_cli.main(port, False, False, False) == 1
    assert launcher.popen == []
    assert launcher.opened == []
    assert capsys.readouterr().out == (
        f"error: another program uses port {port}. Run pdt gui --port {port + 1} "
        "to use another port.\n")


@pytest.fixture
def other_server():
    """An HTTP server of another program on a free port."""
    server = http.server.HTTPServer(("127.0.0.1", 0), http.server.SimpleHTTPRequestHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server.server_address[1]
    server.shutdown()
    server.server_close()


def test_a_stale_pid_file_and_another_program_on_its_port_open_nothing(
        project, launcher, capsys, monkeypatch, other_server):
    monkeypatch.setattr(gui_cli, "is_up", REAL_IS_UP)
    (project / ".pdt").mkdir()
    (project / ".pdt" / "gui.pid").write_text(json.dumps({"pid": 1, "port": other_server}))
    assert gui_cli.main(other_server, False, False, False) == 1
    assert launcher.opened == []
    assert launcher.popen == []
    assert "already running" not in capsys.readouterr().out


def test_only_the_projects_own_server_counts_as_up(project, monkeypatch, other_server):
    assert REAL_IS_UP(other_server, project) is False

    class Pdt(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            body = str(project).encode() if self.path == "/pdt/" else b"?"
            self.send_response(200)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Pdt)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        assert REAL_IS_UP(server.server_address[1], project) is True
        assert REAL_IS_UP(server.server_address[1], project / "other") is False
    finally:
        server.shutdown()
        server.server_close()
