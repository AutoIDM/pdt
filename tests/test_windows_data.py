import base64
from pathlib import Path

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
        f"use folder {folder / 'storage'} for the app's files (kept after destroy)",
    ]


def test_plan_without_storage_has_no_storage_line(project):
    app = windows_app(project, "schedule: hourly\ntimezone: local\nstorage: false\n")
    actions = deploy_windows.plan(app, "create", "hourly at minute 00", r"PC\jon",
                                  "uv.exe", False)
    assert len(actions) == 6
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
    assert kw == {"elevate": True}
    assert script.index("New-Item") < script.index("icacls") < script.index(
        "Register-ScheduledTask")


def test_deploy_ends_with_the_next_pdt_commands(project, monkeypatch, capsys):
    app = windows_app(project)
    monkeypatch.setattr(deploy_windows, "_preflight",
                        lambda require_uv=True: ("powershell.exe", "uv.exe"))
    monkeypatch.setattr(deploy_windows, "_task_state", lambda powershell, name: "absent")
    monkeypatch.setattr(deploy_windows, "_deploying_user", lambda: r"PC\jon")
    monkeypatch.setattr(deploy_windows, "_run", lambda powershell, script, **kw: True)
    assert deploy_windows.deploy(app, assume_yes=True) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[out.index("Next steps:") + 1:] == [
        "  pdt run my-report --deployed  start a run now",
        "  pdt logs my-report            read the log of the newest run",
        "  pdt runs my-report            list the recent runs",
        "  pdt health my-report          show whether the last run succeeded",
    ]



def test_deploy_says_what_it_does_before_each_slow_step(project, monkeypatch, capsys):
    app = windows_app(project)
    monkeypatch.setattr(deploy_windows, "_preflight",
                        lambda require_uv=True: ("powershell.exe", "uv.exe"))
    monkeypatch.setattr(deploy_windows, "_task_state", lambda powershell, name: "managed")
    monkeypatch.setattr(deploy_windows, "_deploying_user", lambda: r"PC\jon")
    monkeypatch.setattr(deploy_windows, "_run", lambda powershell, script, **kw: True)
    assert deploy_windows.deploy(app, assume_yes=True) == 0
    out = capsys.readouterr().out
    assert out.index("Checking Windows scheduled task pdt-my-report") < out.index("Plan")
    assert out.index("Plan") < out.index("Updating Windows scheduled task pdt-my-report")

@pytest.fixture
def powershell_deploy(project, monkeypatch):
    from pdt import powershell
    folder = project / "ps-report"
    folder.mkdir()
    (folder / "config.yml").write_text("schedule: hourly\ntimezone: local\n")
    (folder / "report.ps1").write_text("")
    needs = []
    host = []
    checked = []
    scripts = []
    runs = []
    monkeypatch.setattr(deploy_windows, "ensure_pwsh", lambda: r"C:\ProgramData\pdt\pwsh\pwsh.exe")
    monkeypatch.setattr(powershell, "scan", lambda app, provider: powershell.ScriptScan(
        ["report.ps1"], [], list(needs), [], list(host))
        if provider == "windows" else pytest.fail(provider))

    def has_module(command, **kwargs):
        script = base64.b64decode(command[-1]).decode("utf-16-le")
        checked.append((command[0], script))
        missing = "ActiveDirectory\n" if "'ActiveDirectory'" in script else ""
        return deploy_windows.subprocess.CompletedProcess(command, 0, missing, "")

    monkeypatch.setattr(deploy_windows.subprocess, "run", has_module)
    monkeypatch.setattr(deploy_windows.regions, "local_currency", lambda: "USD")
    monkeypatch.setattr(deploy_windows, "_preflight",
                        lambda require_uv=True: ("powershell.exe", "uv.exe"))
    monkeypatch.setattr(deploy_windows, "_task_state", lambda powershell, name: "absent")
    monkeypatch.setattr(deploy_windows, "_deploying_user", lambda: r"PC\jon")
    monkeypatch.setattr(deploy_windows, "_run",
                        lambda powershell, script, **kw: runs.append(powershell) or scripts.append(script) or True)
    return config.merged_app("ps-report"), needs, host, checked, scripts, runs


def test_powershell_deploy_installs_the_gallery_modules_in_the_elevated_step(
        powershell_deploy, monkeypatch):
    from pdt import powershell
    app, needs, host, checked, scripts, _runs = powershell_deploy
    needs.append(powershell.ModuleNeed("ImportExcel", None, "#Requires in report.ps1"))
    host.append("ScheduledTasks")
    actions = []
    monkeypatch.setattr(deploy_windows, "confirm",
                        lambda lines, assume_yes, cost=None: actions.extend(lines) or True)

    assert deploy_windows.deploy(app, assume_yes=True) == 0

    assert actions[-2:] == [
        r"run the app's .ps1 files with PowerShell from C:\ProgramData\pdt\pwsh\pwsh.exe",
        "install PowerShell modules ImportExcel (all users)"]
    assert checked == [(r"C:\ProgramData\pdt\pwsh\pwsh.exe",
                        "$have = Get-Module -ListAvailable -Name 'ScheduledTasks' | "
                        "Select-Object -ExpandProperty Name; "
                        "'ScheduledTasks' | Where-Object { $have -notcontains $_ }")]
    (script,) = scripts
    install = powershell.install_command([needs[0]])
    encoded = base64.b64encode(install.encode("utf-16-le")).decode("ascii")
    assert (r"& 'C:\ProgramData\pdt\pwsh\pwsh.exe' -NoProfile -NonInteractive "
            f"-EncodedCommand {encoded}; if ($LASTEXITCODE -ne 0) {{ throw") in script
    assert script.index("icacls") < script.index("-EncodedCommand") < script.index(
        "Register-ScheduledTask")


def test_powershell_deploy_without_modules_installs_nothing(powershell_deploy):
    app, _needs, _host, checked, scripts, _runs = powershell_deploy
    assert deploy_windows.deploy(app, assume_yes=True) == 0
    (script,) = scripts
    assert "EncodedCommand" not in script
    assert checked == []


def test_powershell_deploy_runs_the_elevated_script_through_windows_powershell(powershell_deploy):
    from pdt import powershell
    app, needs, _host, _checked, _scripts, runs = powershell_deploy
    needs.append(powershell.ModuleNeed("ImportExcel", None, "#Requires in report.ps1"))
    assert deploy_windows.deploy(app, assume_yes=True) == 0
    assert runs == ["powershell.exe"]


def test_powershell_deploy_names_the_windows_feature_of_a_missing_module(
        powershell_deploy, capsys):
    app, _needs, host, _checked, scripts, _runs = powershell_deploy
    host.append("ActiveDirectory")
    assert deploy_windows.deploy(app, assume_yes=True) == 1
    assert scripts == []
    out = " ".join(capsys.readouterr().out.split())
    assert "the PowerShell module ActiveDirectory, which this PC does not have" in out
    assert "RSAT: Active Directory Domain Services and Lightweight Directory Services Tools" in out
