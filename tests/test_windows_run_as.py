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


@pytest.mark.parametrize("value", ["", ACCOUNT, "svc-pdt$", " EXAMPLE\\svc-pdt$ "])
def test_a_group_managed_service_account_passes(value):
    assert config.run_as_problem(value) == ""


@pytest.mark.parametrize("value", ["EXAMPLE\\svc-pdt", "jon", "$", "EXAMPLE\\$", "svc-pdt$@example.com"])
def test_a_name_without_the_trailing_dollar_is_reported(value):
    assert "group managed service account" in config.run_as_problem(value)


def test_pdt_yml_sets_run_as_for_every_app_and_an_app_overrides_it(project):
    assert deploy_windows.run_as(windows_app(project)) == ACCOUNT
    add_app(project, "other", "schedule: hourly\nplatform:\n  run_as: EXAMPLE\\svc-other$\n")
    assert deploy_windows.run_as(config.merged_app("other")) == "EXAMPLE\\svc-other$"


def test_validate_names_the_key_for_an_account_with_a_password(project):
    (project / "pdt.yml").write_text("platform:\n  provider: windows\n  run_as: EXAMPLE\\jon\n")
    add_app(project, "my-report", "schedule: daily\n")
    problems = config.validate_app("my-report")
    assert any(p.startswith("my-report: platform.run_as: That is not a group managed service "
                            "account") for p in problems)


def test_validate_reports_run_as_on_a_cloud_provider(project):
    (project / "pdt.yml").write_text("platform:\n  provider: aws\n  run_as: EXAMPLE\\svc-pdt$\n")
    add_app(project, "my-report", "schedule: daily\n")
    assert ("my-report: platform.run_as: only the windows provider uses it; remove the key"
            in config.validate_app("my-report"))


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


def test_the_account_reads_the_project_pdt_uv_and_the_installation_key(project):
    app = windows_app(project)
    key = project / "ProgramData" / "pdt" / "installation.key"
    key.parent.mkdir(parents=True)
    key.write_text("")
    reads = deploy_windows.account_reads(app, "uv.exe", False)
    assert reads == [
        ("the project folder", project, "(OI)(CI)RX"),
        ("pdt", deploy_windows.RUNNER.parent.parent, "(OI)(CI)RX"),
        ("uv", Path("uv.exe").resolve().parent, "(OI)(CI)RX"),
        ("the AutoIDM installation key", key, "R"),
    ]
    script = deploy_windows._folder_script(app, r"PC\jon", reads)
    assert f"icacls '{project}' /grant 'EXAMPLE\\svc-pdt$:(OI)(CI)RX' | Out-Null" in script
    assert f"icacls '{key}' /grant 'EXAMPLE\\svc-pdt$:R' | Out-Null" in script
    assert script.count("throw") == 5


def test_the_account_reads_no_uv_on_the_machine_path_and_no_missing_key(project):
    reads = deploy_windows.account_reads(windows_app(project), "uv.exe", True)
    assert [what for what, _path, _rights in reads] == ["the project folder", "pdt"]


def test_system_needs_no_read_grants(project):
    (project / "pdt.yml").write_text("platform:\n  provider: windows\n")
    app = windows_app(project)
    assert deploy_windows.account_reads(app, "uv.exe", False) == []
    assert deploy_windows._folder_script(app, r"PC\jon").count("/grant") == 1


def deploy(project, monkeypatch, problem=""):
    app = windows_app(project)
    shown, scripts = [], []
    monkeypatch.setattr(deploy_windows, "_preflight", lambda: ("powershell.exe", "uv.exe"))
    monkeypatch.setattr(deploy_windows, "_task_state", lambda powershell, name: "managed")
    monkeypatch.setattr(deploy_windows, "_deploying_user", lambda: r"PC\jon")
    monkeypatch.setattr(deploy_windows, "uv_on_machine_path", lambda: True)
    monkeypatch.setattr(deploy_windows, "_account_problem", lambda powershell, account: problem)
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
    assert f"let {ACCOUNT} read the project folder {project}" in shown
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
