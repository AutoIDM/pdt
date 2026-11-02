import base64
import re
import subprocess
from pathlib import Path

import pytest

from conftest import add_app, plain
from pdt import config, deploy_windows

ACCOUNT = "EXAMPLE\\svc-pdt$"


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.delenv("PDT_PROJECT", raising=False)
    (tmp_path / "pdt.yml").write_text("platform:\n  provider: windows\n  run_as: EXAMPLE\\svc-pdt$\n")
    monkeypatch.setenv("ProgramData", str(tmp_path / "ProgramData"))
    monkeypatch.chdir(tmp_path)
    return tmp_path


def windows_app(project, config_text="schedule: hourly\n"):
    add_app(project, "my-report", config_text)
    return config.merged_app("my-report")


@pytest.mark.parametrize("value", [None, "", ACCOUNT, "svc-pdt$", " EXAMPLE\\svc-pdt$ "])
def test_a_group_managed_service_account_passes(value):
    assert config.run_as_problem(value) == ""


@pytest.mark.parametrize("value", ["EXAMPLE\\svc-pdt", "jon", "$", "EXAMPLE\\$", "svc-pdt$@example.com",
                                   False, 0, [], {}, True])
def test_a_value_that_is_not_a_name_ending_in_dollar_is_reported(value):
    assert "group managed service account" in config.run_as_problem(value)


@pytest.mark.parametrize("value", ["false", "0", "[]", "{}"])
def test_validate_reports_a_run_as_that_is_not_text(project, value):
    add_app(project, "my-report", f"schedule: daily\nplatform:\n  run_as: {value}\n")
    assert any(p.startswith("my-report/config.yml: platform.run_as: That is not a group managed "
                            "service account") for p in config.validate_app("my-report"))


@pytest.mark.parametrize("name", ["pwsh", "PWSH", "versions", "wheels", "installation.key",
                                  "pwsh.previous", "pwsh."])
def test_validate_reports_a_windows_app_named_like_a_pdt_folder(project, name):
    add_app(project, name, "schedule: daily\n")
    assert any(p.startswith(f"{name}/: a Windows app keeps its run data in ")
               and f"rename the folder {name}/" in p for p in config.validate_app(name))


def test_an_app_named_like_a_pdt_folder_is_fine_on_a_cloud_provider(project):
    (project / "pdt.yml").write_text("platform:\n  provider: aws\n")
    add_app(project, "pwsh", "schedule: daily\n")
    assert not any("run data" in p for p in config.validate_app("pwsh"))


def test_pdt_yml_sets_run_as_for_every_app_and_an_app_overrides_it(project):
    assert deploy_windows.run_as(windows_app(project)) == ACCOUNT
    add_app(project, "other", "schedule: hourly\nplatform:\n  run_as: EXAMPLE\\svc-other$\n")
    assert deploy_windows.run_as(config.merged_app("other")) == "EXAMPLE\\svc-other$"


def test_validate_names_the_key_for_an_account_with_a_password(project):
    (project / "pdt.yml").write_text("platform:\n  provider: windows\n  run_as: EXAMPLE\\jon\n")
    add_app(project, "my-report", "schedule: daily\n")
    problems = config.validate_app("my-report")
    assert any(p.startswith("pdt.yml: platform.run_as: That is not a group managed service "
                            "account") for p in problems)


def test_validate_reports_run_as_on_a_cloud_provider(project):
    (project / "pdt.yml").write_text("platform:\n  provider: aws\n  run_as: EXAMPLE\\svc-pdt$\n")
    add_app(project, "my-report", "schedule: daily\n")
    assert ("pdt.yml: platform.run_as: only the windows provider uses it; remove the key"
            in config.validate_app("my-report"))


def test_run_as_from_names_the_most_specific_file_that_sets_it(project):
    assert windows_app(project)["run_as_from"] == "pdt.yml"
    add_app(project, "other", "schedule: hourly\nplatform:\n  run_as: EXAMPLE\\svc-other$\n")
    assert config.merged_app("other")["run_as_from"] == "other/config.yml"


def test_validate_accepts_a_group_managed_service_account(project):
    add_app(project, "my-report", "schedule: daily\n")
    assert not any("run_as" in p for p in config.validate_app("my-report"))


def test_without_run_as_the_task_runs_as_system(project):
    (project / "pdt.yml").write_text("platform:\n  provider: windows\n")
    _description, xml = deploy_windows.task_xml(windows_app(project), "uv.exe", False)
    assert "<UserId>S-1-5-18</UserId>" in xml
    assert "LogonType" not in xml


def test_with_run_as_the_task_logs_on_with_the_domain_password(project):
    _description, xml = deploy_windows.task_xml(windows_app(project), "uv.exe", False)
    assert ("<Principal id=\"Author\">\n      <UserId>EXAMPLE\\svc-pdt$</UserId>\n"
            "      <LogonType>Password</LogonType>\n"
            "      <RunLevel>HighestAvailable</RunLevel>") in xml
    assert "S-1-5-18" not in xml


def test_the_account_gets_full_control_of_the_app_folder(project):
    script = deploy_windows._folder_script(windows_app(project), r"PC\jon")
    assert ("icacls $folder /grant '*S-1-5-18:(OI)(CI)F' 'EXAMPLE\\svc-pdt$:(OI)(CI)F' "
            "'PC\\jon:(OI)(CI)M'") in script


def test_the_account_reads_its_app_the_project_files_pdt_uv_and_the_installation_key(project):
    app = windows_app(project)
    add_app(project, "other", "schedule: hourly\n")
    (project / ".env").write_text("")
    (project / "pyproject.toml").write_text("")
    key = project / "ProgramData" / "pdt" / "installation.key"
    key.parent.mkdir(parents=True)
    key.write_text("")
    reads = deploy_windows.account_reads(app, "uv.exe", False)
    assert reads == [
        ("the app folder", project / "my-report", "(OI)(CI)RX"),
        ("the project folder entry", project, "RX"),
        ("the project file pdt.yml", project / "pdt.yml", "R"),
        ("the project file .env", project / ".env", "R"),
        ("the project file pyproject.toml", project / "pyproject.toml", "R"),
        ("pdt", deploy_windows.RUNNER.parent.parent, "(OI)(CI)RX"),
        ("uv", Path("uv.exe").resolve().parent, "(OI)(CI)RX"),
        ("the AutoIDM installation key", key, "R"),
    ]
    assert not any(path == project / "other" for _what, path, _rights in reads)
    script = deploy_windows._folder_script(app, r"PC\jon", reads)
    assert f"icacls '{project}' /grant 'EXAMPLE\\svc-pdt$:RX' | Out-Null" in script
    assert f"icacls '{key}' /grant 'EXAMPLE\\svc-pdt$:R' | Out-Null" in script
    assert script.count("throw") == 9


def test_the_account_reads_no_uv_on_the_machine_path_and_no_missing_file(project):
    reads = deploy_windows.account_reads(windows_app(project), "uv.exe", True)
    assert [what for what, _path, _rights in reads] == [
        "the app folder", "the project folder entry", "the project file pdt.yml", "pdt"]


def test_the_account_reads_the_shared_pwsh_install_and_no_other_pwsh(project):
    app = windows_app(project)
    shared = str(project / "ProgramData" / "pdt" / "pwsh" / "pwsh.exe")
    reads = deploy_windows.account_reads(app, "uv.exe", True, shared)
    assert ("PowerShell", project / "ProgramData" / "pdt" / "pwsh", "(OI)(CI)RX") in reads
    reads = deploy_windows.account_reads(app, "uv.exe", True, r"C:\Program Files\PowerShell\7\pwsh.exe")
    assert "PowerShell" not in [what for what, _path, _rights in reads]


@pytest.mark.parametrize("on_machine_path, account, expected", [
    (False, "", True), (True, ACCOUNT, True), (False, ACCOUNT, False)])
def test_a_run_as_powershell_app_uses_pwsh_from_the_machine_path_or_the_shared_install(
        project, monkeypatch, on_machine_path, account, expected):
    platform = f"platform:\n  run_as: {account}\n" if account else "platform:\n  run_as:\n"
    windows_app(project, "schedule: hourly\n" + platform)
    (project / "my-report" / "run.py").unlink()
    (project / "my-report" / "run.ps1").write_text("Write-Output hi\n")
    asked = []
    monkeypatch.setattr(deploy_windows, "machine_path_has", lambda program: on_machine_path)
    monkeypatch.setattr(deploy_windows, "ensure_pwsh", lambda on_path: asked.append(on_path) or "pwsh")
    monkeypatch.setattr(deploy_windows.ps, "scan", lambda app, target: type("Scan", (), {
        "host_modules": [], "modules": []})())
    monkeypatch.setattr(deploy_windows, "_missing_host_modules", lambda pwsh, modules: [])
    assert deploy_windows._prepare_powershell(config.merged_app("my-report")) == ("pwsh", [])
    assert asked == [expected]


def test_pwsh_off_path_skips_the_path_and_takes_the_shared_install(tmp_path, monkeypatch):
    from pdt import pwsh
    monkeypatch.setattr(pwsh.shutil, "which", lambda name: "/usr/bin/pwsh")
    monkeypatch.setattr(pwsh, "install_dir", lambda windows=False: tmp_path)
    (tmp_path / "pwsh").write_text("")
    (tmp_path / "VERSION").write_text(pwsh.VERSION)
    assert pwsh.ensure_pwsh() == "/usr/bin/pwsh"
    assert pwsh.ensure_pwsh(on_path=False) == str(tmp_path / "pwsh")


def account_check(monkeypatch, answer):
    scripts = []
    monkeypatch.setattr(deploy_windows, "_run_output",
                        lambda powershell, script: scripts.append(script) or f"{answer}\r\n")
    problem = deploy_windows._account_problem("powershell.exe", ACCOUNT, "pdt.yml")
    return problem, scripts[0]


def test_the_account_check_asks_active_directory_for_the_gmsa_class(monkeypatch):
    problem, script = account_check(monkeypatch, "gmsa")
    assert problem == ""
    assert "[Security.Principal.NTAccount]'EXAMPLE\\svc-pdt$'" in script
    assert ("[adsisearcher]\"(&(objectClass=msDS-GroupManagedServiceAccount)(objectSid=$sid))\""
            in script)


@pytest.mark.parametrize("answer, start", [
    ("missing", f"Windows on this computer cannot find the account {ACCOUNT}."),
    ("not-gmsa", f"{ACCOUNT} is not a group managed service account."),
    ("no-domain", f"pdt cannot ask the domain whether {ACCOUNT}"),
])
def test_the_account_check_names_the_file_that_sets_run_as(monkeypatch, answer, start):
    problem, _script = account_check(monkeypatch, answer)
    assert problem.startswith(start)
    assert "platform.run_as" in problem and "pdt.yml" in problem


def test_system_needs_no_read_grants(project, monkeypatch):
    (project / "pdt.yml").write_text("platform:\n  provider: windows\n")
    _code, shown, (script,) = deploy(project, monkeypatch)
    assert script.count("/grant") == 1
    assert "/remove" not in script
    assert not any(line.startswith("let ") for line in shown)


def deploy(project, monkeypatch, problem="", previous=""):
    app = windows_app(project)
    shown, scripts = [], []
    monkeypatch.setattr(deploy_windows, "_preflight", lambda: ("powershell.exe", "uv.exe"))
    monkeypatch.setattr(deploy_windows, "_task_state", lambda powershell, name: "managed")
    monkeypatch.setattr(deploy_windows, "_previous_account",
                        lambda powershell, name, account: previous)
    monkeypatch.setattr(deploy_windows, "_deploying_user", lambda: r"PC\jon")
    monkeypatch.setattr(deploy_windows, "machine_path_has", lambda program: True)
    monkeypatch.setattr(deploy_windows, "_account_problem",
                        lambda powershell, account, where: problem)
    monkeypatch.setattr(deploy_windows.regions, "local_currency", lambda: "USD")
    monkeypatch.setattr(deploy_windows, "confirm",
                        lambda actions, assume_yes, cost: shown.extend(plain(actions)) or True)
    monkeypatch.setattr(deploy_windows, "_run",
                        lambda powershell, script, **kw: scripts.append(script) or True)
    return deploy_windows.deploy(app, assume_yes=True), shown, scripts


def test_deploy_plans_the_account_and_states_the_batch_right(project, monkeypatch, capsys):
    code, shown, scripts = deploy(project, monkeypatch)
    assert code == 0
    folder = project / "ProgramData" / "pdt" / "my-report"
    assert shown[0] == f"update Windows scheduled task pdt-my-report (runs as {ACCOUNT})"
    assert (f"keep the app's run data in {folder} (SYSTEM and {ACCOUNT}: full control; "
            "PC\\jon: modify)") in shown
    assert f"let {ACCOUNT} read the app folder {project / 'my-report'}" in shown
    assert f"let {ACCOUNT} read the project file pdt.yml {project / 'pdt.yml'}" in shown
    assert f"let {ACCOUNT} read pdt {deploy_windows.RUNNER.parent.parent}" in shown
    out = capsys.readouterr().out
    assert (f"note: the task starts only when {ACCOUNT} has the Log on as a batch job right on "
            "this computer. pdt does not grant this right.") in " ".join(out.split())
    (script,) = scripts
    assert script.index(f"icacls '{project}'") < script.index("Register-ScheduledTask -TaskName "
                                                              "$name -Xml $xml -Force")


def test_deploy_stops_when_windows_cannot_find_the_account(project, monkeypatch, capsys):
    code, shown, scripts = deploy(project, monkeypatch, problem="no such account")
    assert (code, shown, scripts) == (1, [], [])
    assert "error: no such account" in capsys.readouterr().out


def test_deploy_without_run_as_says_nothing_about_the_batch_right(project, monkeypatch, capsys):
    (project / "pdt.yml").write_text("platform:\n  provider: windows\n")
    code, shown, _scripts = deploy(project, monkeypatch)
    assert code == 0
    assert shown[0] == "update Windows scheduled task pdt-my-report (runs as SYSTEM)"
    assert "batch" not in capsys.readouterr().out


@pytest.mark.parametrize("account, new", [
    (ACCOUNT, "([Security.Principal.NTAccount]'EXAMPLE\\svc-pdt$').Translate("),
    ("", "'S-1-5-18'")])
def test_the_previous_account_is_neither_system_nor_the_new_account(monkeypatch, account, new):
    scripts = []
    monkeypatch.setattr(deploy_windows, "_run_output",
                        lambda powershell, script: scripts.append(script) or "EXAMPLE\\svc-old$\r\n")
    assert deploy_windows._previous_account("powershell.exe", "pdt-my-report",
                                            account) == "EXAMPLE\\svc-old$"
    assert "$old = $task.Principal.UserId; " in scripts[0]
    assert f"if ($sid -ne 'S-1-5-18' -and $sid -ne {new}" in scripts[0]


def test_deploy_removes_the_rights_of_the_account_the_task_ran_as_before(project, monkeypatch):
    (project / "pdt.yml").write_text("platform:\n  provider: windows\n")
    folder = project / "ProgramData" / "pdt" / "my-report"
    folder.mkdir(parents=True)
    code, shown, (script,) = deploy(project, monkeypatch, previous="EXAMPLE\\svc-old$")
    assert code == 0
    assert ("remove the rights pdt gave EXAMPLE\\svc-old$, the account the task ran as until now"
            in shown)
    for path in (folder, project / "my-report", project, project / "pdt.yml",
                 deploy_windows.RUNNER.parent.parent):
        assert f"icacls '{path}' /remove:g 'EXAMPLE\\svc-old$' | Out-Null" in script
    assert script.rindex("/remove:g") < script.index("/grant")


def test_destroy_removes_the_rights_of_the_account_the_task_runs_as(project, monkeypatch):
    folder = project / "ProgramData" / "pdt" / "my-report"
    folder.mkdir(parents=True)
    app = windows_app(project)
    shown, scripts, asked = [], [], []
    monkeypatch.setattr(deploy_windows, "_preflight",
                        lambda require_uv=True: ("powershell.exe", None))
    monkeypatch.setattr(deploy_windows, "_task_state", lambda powershell, name: "managed")
    monkeypatch.setattr(deploy_windows, "_previous_account",
                        lambda powershell, name, account: asked.append(account) or ACCOUNT)
    monkeypatch.setattr(deploy_windows, "confirm",
                        lambda actions, assume_yes: shown.extend(plain(actions)) or True)
    monkeypatch.setattr(deploy_windows, "_run",
                        lambda powershell, script, **kw: scripts.append(script) or True)
    assert deploy_windows.destroy(app, assume_yes=True) == 0
    assert asked == [""]
    assert f"remove the rights pdt gave {ACCOUNT}, the account the task runs as" in shown
    (script,) = scripts
    assert f"icacls '{folder}' /remove:g '{ACCOUNT}' | Out-Null" in script
    assert script.index("Unregister-ScheduledTask") < script.index("/remove:g")


@pytest.mark.parametrize("returncode", [0, 1])
def test_an_elevated_script_passes_as_a_file_that_is_deleted_afterwards(tmp_path, monkeypatch,
                                                                        returncode):
    temp = tmp_path / "Temp Folder"
    temp.mkdir()
    monkeypatch.setattr(deploy_windows.tempfile, "tempdir", str(temp))
    monkeypatch.setattr(deploy_windows, "_is_admin", lambda: False)
    script = f"icacls '{'C:\\long folder\\' * 2000}' /grant 'EXAMPLE\\svc-pdt$:R'"
    passed = []

    def run(command, **kwargs):
        outer = base64.b64decode(command[-1]).decode("utf-16-le")
        file = Path(re.search(r"'-File','\"(.+)\"'; exit \$p\.ExitCode$", outer).group(1))
        passed.append((file, file.read_text(encoding="utf-8-sig"),
                       len(subprocess.list2cmdline(command))))
        return subprocess.CompletedProcess(command, returncode, "", "")

    monkeypatch.setattr(deploy_windows.subprocess, "run", run)
    if returncode == 0:
        assert deploy_windows._run("powershell.exe", script, elevate=True)
    else:
        with pytest.raises(deploy_windows.WindowsDeployError):
            deploy_windows._run("powershell.exe", script, elevate=True)
    ((file, text, length),) = passed
    assert text == script
    assert length < 2000 < len(script)
    assert list(temp.iterdir()) == []
