
from conftest import add_app
from pdt import deploy_windows, runs_cli


def captured(monkeypatch):
    calls = []
    monkeypatch.setattr(runs_cli, "logs", lambda *args, **kwargs: calls.append(kwargs) or 0)
    return calls


def test_windows_reads_a_local_file_with_no_delay(project, monkeypatch):
    (project / "pdt.yml").write_text("platform:\n  provider: windows\n")
    add_app(project, "report")
    calls = captured(monkeypatch)
    monkeypatch.setattr(deploy_windows.sys, "platform", "win32")
    monkeypatch.setattr("sys.argv", ["deploy_windows.py", "logs", "report"])
    assert deploy_windows.main() == 0
    assert "delay" not in calls[0]
