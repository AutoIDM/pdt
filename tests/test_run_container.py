import subprocess

from pdt import console, run_container


class Server:
    """A server process whose first waits raise the given exceptions."""

    def __init__(self, *raises):
        self.raises = list(raises)

    def wait(self, timeout=None):
        if self.raises:
            raise self.raises.pop(0)
        return 0


def test_a_second_ctrl_c_while_the_server_stops_still_kills_its_group(monkeypatch):
    calls = []
    monkeypatch.setattr(run_container, "interrupt", lambda process: calls.append("interrupt"))
    monkeypatch.setattr(run_container, "kill", lambda process: calls.append("kill"))
    monkeypatch.setattr(console, "done", lambda message: calls.append("done"))
    monkeypatch.setattr(subprocess, "run", lambda args, **kw: calls.append(args[:3]))
    server = Server(KeyboardInterrupt(), KeyboardInterrupt())
    assert run_container.serve("web-hello", server, "pdt-web-hello-local-1") == 0
    assert calls == ["interrupt", "kill", "done", ["docker", "rm", "--force"]]
