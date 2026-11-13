import re
from datetime import datetime, timezone

import pytest

from conftest import add_app
from pdt import config, deploy_windows, runs_cli


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.delenv("PDT_PROJECT", raising=False)
    (tmp_path / "pdt.yml").write_text("platform:\n  provider: windows\n")
    monkeypatch.setenv("ProgramData", str(tmp_path / "ProgramData"))
    monkeypatch.chdir(tmp_path)
    return tmp_path


def settings_enabled(xml: str) -> str:
    settings = xml[xml.index("<Settings>"):xml.index("</Settings>")]
    return re.search(r"<Enabled>(\w+)</Enabled>", settings).group(1)


def test_a_paused_app_registers_a_disabled_task(project):
    add_app(project, "my-report", "schedule: hourly\ntimezone: local\npause: true\n")
    _description, xml = deploy_windows.task_xml(config.merged_app("my-report"), "uv.exe", False)
    assert settings_enabled(xml) == "false"
    assert "<StartBoundary>2000-01-01T00:00:00</StartBoundary><Enabled>true</Enabled>" in xml


def test_an_app_that_is_not_paused_registers_an_enabled_task(project):
    add_app(project, "my-report", "schedule: hourly\ntimezone: local\n")
    _description, xml = deploy_windows.task_xml(config.merged_app("my-report"), "uv.exe", False)
    assert settings_enabled(xml) == "true"


def fake_task(monkeypatch, state: str):
    scripts = []
    monkeypatch.setattr(deploy_windows, "_preflight", lambda require_uv=True: ("powershell.exe", None))
    monkeypatch.setattr(deploy_windows, "_task_state", lambda powershell, name: state)
    monkeypatch.setattr(deploy_windows, "_run",
                        lambda powershell, script, **kwargs: scripts.append((script, kwargs)) or True)
    return scripts


def test_pause_disables_and_unpause_enables_the_task(monkeypatch, capsys):
    scripts = fake_task(monkeypatch, "managed")
    assert deploy_windows.pause({"name": "my-report"}, True) == 0
    assert deploy_windows.pause({"name": "my-report"}, False) == 0
    assert scripts[0][0].startswith("Disable-ScheduledTask -TaskName 'pdt-my-report'")
    assert scripts[1][0].startswith("Enable-ScheduledTask -TaskName 'pdt-my-report'")
    assert all(kwargs == {"elevate": True} for _script, kwargs in scripts)
    out = capsys.readouterr().out
    assert "Paused my-report" in out
    assert "Unpaused my-report" in out


def test_start_runs_the_task_now_and_marks_the_run_manual(monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("ProgramData", str(tmp_path))
    scripts = fake_task(monkeypatch, "managed")
    assert deploy_windows.start({"name": "my-report"}) == 0
    assert (deploy_windows.logs_folder("my-report") / "manual-start").is_file()
    assert scripts[0][0].startswith("Start-ScheduledTask -TaskName 'pdt-my-report'")
    assert scripts[0][1] == {"elevate": True}
    assert "Started my-report" in capsys.readouterr().out


def test_start_with_follow_waits_for_the_new_log_before_it_reads_the_run_list(
        monkeypatch, tmp_path):
    monkeypatch.setenv("ProgramData", str(tmp_path))
    fake_task(monkeypatch, "managed")
    folder = deploy_windows.logs_folder("my-report")
    folder.mkdir(parents=True)
    (folder / "20260923T090000Z.log").write_text("pdt: exit 0\n")
    naps = []

    def runner_starts(seconds):
        naps.append(seconds)
        if len(naps) == 3:
            (folder / "20260923T100000Z.log").write_text("")
    monkeypatch.setattr(deploy_windows.time, "sleep", runner_starts)
    followed = []
    monkeypatch.setattr(deploy_windows, "logs", lambda app, rest, new: followed.append(
        [path.stem for path in folder.glob("*.log") if new(runs_cli.Run(path.stem, datetime.now(timezone.utc), None, "running"))])
        or 0)

    assert deploy_windows.start({"name": "my-report"}, follow=True) == 0
    assert naps == [deploy_windows.FOLLOW_POLL_SECONDS] * 3
    assert followed == [["20260923T100000Z"]]


def test_a_missing_task_says_deploy_first(monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("ProgramData", str(tmp_path))
    scripts = fake_task(monkeypatch, "absent")
    assert deploy_windows.pause({"name": "my-report"}, True) == 1
    assert deploy_windows.start({"name": "my-report"}) == 1
    assert scripts == []
    assert "run pdt deploy my-report first" in capsys.readouterr().out


def test_a_task_pdt_does_not_manage_is_refused(monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("ProgramData", str(tmp_path))
    scripts = fake_task(monkeypatch, "unmanaged")
    assert deploy_windows.start({"name": "my-report"}) == 1
    assert scripts == []
    assert "not managed by PDT" in capsys.readouterr().out
