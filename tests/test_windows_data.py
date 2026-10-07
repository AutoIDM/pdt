from pathlib import Path
import subprocess

import pytest

from conftest import add_app
from pdt import config, deploy_windows


def test_machine_data_home_is_pdt_under_program_data(monkeypatch):
    monkeypatch.setenv("ProgramData", r"D:\ProgramData")
    assert config.machine_data_home() == Path(r"D:\ProgramData") / "pdt"


def test_machine_data_home_falls_back_to_the_system_drive(monkeypatch):
    monkeypatch.delenv("ProgramData", raising=False)
    assert config.machine_data_home() == Path(r"C:\ProgramData") / "pdt"


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.delenv("PDT_PROJECT", raising=False)
    (tmp_path / "pdt.yml").write_text("platform:\n  provider: windows\n")
    monkeypatch.setenv("ProgramData", str(tmp_path / "ProgramData"))
    monkeypatch.chdir(tmp_path)
    return tmp_path


def windows_app(project, config_text="schedule: hourly\ntimezone: local\n"):
    add_app(project, "my-report", config_text)
    return config.merged_app("my-report")


@pytest.mark.parametrize(("command", "rest"), [
    ("deploy", []),
    ("destroy", []),
    ("login", []),
    ("secrets", ["diff"]),
    ("storage", ["ls", "state/"]),
    ("runs", ["--", "--json"]),
    ("logs", ["--", "1"]),
])
def test_local_windows_commands_stop_off_windows(
        project, monkeypatch, capsys, command, rest):
    windows_app(project)
    monkeypatch.setattr(deploy_windows.sys, "platform", "darwin")
    monkeypatch.setattr(deploy_windows.sys, "argv",
                        ["deploy_windows.py", command, "my-report", *rest])

    assert deploy_windows.main() == 1

    assert capsys.readouterr().out == (
        "error: the windows provider targets this computer, but the current operating "
        "system is not Windows. Run this command on the Windows PC.\n")


def test_app_folders_live_under_the_machine_data_home(project):
    folder = project / "ProgramData" / "pdt" / "my-report"
    assert deploy_windows.app_folder("my-report") == folder
    assert deploy_windows.storage_folder("my-report") == folder / "storage"
    assert deploy_windows.logs_folder("my-report") == folder / "logs"


def test_plan_names_the_data_folder_its_rules_and_both_subfolders(project):
    app = windows_app(project)
    folder = project / "ProgramData" / "pdt" / "my-report"
    actions = deploy_windows.plan(app, "create", "hourly at minute 00", r"PC\jon",
                                  "uv.exe", False)
    assert actions == [
        "create Windows scheduled task pdt-my-report (runs as SYSTEM)",
        "run my-report hourly at minute 00 (machine local time)",
        f"working directory: {app['dir']}",
        f"run uv from {project / 'uv.exe'} (uv is not on the system PATH; "
        "a machine-wide install drops the path from the task)",
        f"keep the app's run data in {folder} (SYSTEM: full control; PC\\jon: modify)",
        f"write one log per run under {folder / 'logs'} (removed on destroy)",
        f"install the failure notice's packages in {folder.parent / 'notify-cache'} "
        "(SYSTEM: full control; PC\\jon: modify)",
        f"use folder {folder / 'storage'} for the app's files (kept after destroy)",
    ]


def test_plan_without_storage_has_no_storage_line(project):
    app = windows_app(project, "schedule: hourly\ntimezone: local\nstorage: false\n")
    actions = deploy_windows.plan(app, "create", "hourly at minute 00", r"PC\jon",
                                  "uv.exe", False)
    assert len(actions) == 7
    assert not any("app's files" in action for action in actions)


def test_folder_script_creates_the_folders_and_grants_the_two_rules(project):
    app = windows_app(project)
    folder = project / "ProgramData" / "pdt" / "my-report"
    script = deploy_windows._folder_script(app, r"PC\jon")
    assert (f"New-Item -ItemType Directory -Force -Path '{folder}', "
            f"'{folder / 'logs'}', '{folder / 'storage'}'") in script
    assert "icacls $folder /grant '*S-1-5-18:(OI)(CI)F' 'PC\\jon:(OI)(CI)M'" in script
    assert script.count("/grant") == 1
    assert "/inheritance" not in script


def test_deploy_creates_the_folders_before_registering_the_task(project, monkeypatch):
    app = windows_app(project)
    scripts = []
    monkeypatch.setattr(deploy_windows, "_preflight",
                        lambda require_uv=True: ("powershell.exe", "uv.exe"))
    monkeypatch.setattr(deploy_windows, "_task_state", lambda powershell, name: "absent")
    monkeypatch.setattr(deploy_windows, "_deploying_user", lambda: r"PC\jon")
    monkeypatch.setattr(deploy_windows, "_run",
                        lambda powershell, script, **kw: scripts.append((script, kw)) or True)
    assert deploy_windows.deploy(app, assume_yes=True) == 0
    (script, kw), = scripts
    assert kw == {"elevate": True, "warn_exit": deploy_windows.NOTICE_CACHE_FAILED}
    assert script.index("New-Item") < script.index("icacls") < script.index(
        "Register-ScheduledTask") < script.index("UV_CACHE_DIR")


def test_deploy_fills_the_notice_cache_with_the_folder_rules(project):
    cache = project / "ProgramData" / "pdt" / "notify-cache"
    script = deploy_windows._notice_cache_script(r"PC\jon", r"C:\tools\uv.exe")
    assert deploy_windows.notice_cache() == cache
    assert f"$cache = '{cache}'; New-Item -ItemType Directory -Force -Path $cache" in script
    assert "icacls $cache /grant '*S-1-5-18:(OI)(CI)F' 'PC\\jon:(OI)(CI)M'" in script
    assert "$env:UV_CACHE_DIR = $cache; " in script
    assert f"& 'C:\\tools\\uv.exe' sync --script '{deploy_windows.NOTICE}'" in script
    assert script.endswith(f"exit {deploy_windows.NOTICE_CACHE_FAILED} }}")


def test_deploy_warns_and_succeeds_when_the_notice_cache_fails(project, monkeypatch, capsys):
    app = windows_app(project)
    monkeypatch.setattr(deploy_windows, "_preflight",
                        lambda require_uv=True: ("powershell.exe", "uv.exe"))
    monkeypatch.setattr(deploy_windows, "_task_state", lambda powershell, name: "absent")
    monkeypatch.setattr(deploy_windows, "_deploying_user", lambda: r"PC\jon")
    monkeypatch.setattr(deploy_windows, "_run", lambda powershell, script, **kw: False)
    assert deploy_windows.deploy(app, assume_yes=True) == 0
    out = capsys.readouterr().out
    assert "Deployed my-report" in out
    assert "a failed run installs them itself" in out


def test_run_returns_false_for_the_warn_exit_code(monkeypatch):
    monkeypatch.setattr(deploy_windows.subprocess, "run",
                        lambda *args, **kwargs: subprocess.CompletedProcess(args, 5, "", ""))
    assert deploy_windows._run("powershell.exe", "exit 5", warn_exit=5) is False
    with pytest.raises(deploy_windows.WindowsDeployError):
        deploy_windows._run("powershell.exe", "exit 5")


def test_deploy_prints_the_run_logs_folder(project, monkeypatch, capsys):
    app = windows_app(project)
    monkeypatch.setattr(deploy_windows, "_preflight",
                        lambda require_uv=True: ("powershell.exe", "uv.exe"))
    monkeypatch.setattr(deploy_windows, "_task_state", lambda powershell, name: "absent")
    monkeypatch.setattr(deploy_windows, "_deploying_user", lambda: r"PC\jon")
    monkeypatch.setattr(deploy_windows, "_run", lambda powershell, script, **kw: True)
    assert deploy_windows.deploy(app, assume_yes=True) == 0
    out = capsys.readouterr().out
    assert f"Run logs: {project / 'ProgramData' / 'pdt' / 'my-report' / 'logs'}" in out
