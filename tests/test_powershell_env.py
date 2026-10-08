"""Each env var a PowerShell app's scripts read is a required env var, on every command path."""

import json
import os
import subprocess
from pathlib import Path

import pytest

from conftest import add_app
from pdt import cli, config, deploy, deploy_common, powershell, run_powershell

SCRIPT = {
    "file": "report.ps1", "parseErrors": [], "requires": None, "usingModules": [],
    "importModules": [], "commands": [], "definedFunctions": [], "params": [],
    "localInvocations": [], "strings": [], "types": [], "newObjects": [], "assemblies": [],
    "dynamic": [], "remoting": [],
    "envReads": [{"name": "TENANT_ID", "line": 4, "set": False},
                 {"name": "NOTE", "line": 5, "set": False}],
}
FACTS = {"files": [SCRIPT], "requirements": None, "known": {}}


@pytest.fixture(autouse=True)
def restore_environ():
    # python-dotenv writes straight into os.environ, past monkeypatch.
    saved = dict(os.environ)
    yield
    os.environ.clear()
    os.environ.update(saved)


@pytest.fixture
def ps_app(project, monkeypatch):
    folder = project / "ps-report"
    folder.mkdir()
    (folder / "report.ps1").write_text("")
    (folder / "config.yml").write_text("schedule: daily\nenv:\n  optional: [NOTE]\n")
    monkeypatch.setattr(powershell, "extract", lambda folder: FACTS)
    monkeypatch.delenv("TENANT_ID", raising=False)
    monkeypatch.delenv("NOTE", raising=False)
    monkeypatch.delenv("PDT_ENV_SECRET_RESOURCE", raising=False)
    return folder


def expected(project):
    return (
        "missing required env var TENANT_ID (report.ps1:4 reads $env:TENANT_ID). pdt looked in "
        f"the environment; there is no .env file in {project / 'ps-report'} or a folder above it. "
        f"To fix it, add each one as NAME=value to {project / '.env'}, then run the command again. "
        "If a script works without one that it reads, list that one under env: optional: in "
        "ps-report/config.yml instead.")


def run_cli(monkeypatch, *argv):
    monkeypatch.setattr("sys.argv", ["pdt", *argv])
    return cli.main()


def test_a_read_that_config_does_not_list_is_required_and_a_listed_one_keeps_its_listing(ps_app):
    assert config.env_spec(config.merged_app("ps-report")) == {
        "optional": ["NOTE"], "required": ["TENANT_ID"], "read_by": {"TENANT_ID": "report.ps1:4"}}


def test_a_python_app_is_not_scanned(project, monkeypatch):
    add_app(project, "py-report", "env:\n  required: [A]\n")
    monkeypatch.setattr(powershell, "extract", lambda folder: pytest.fail("scanned"))
    assert config.env_spec(config.merged_app("py-report")) == {"required": ["A"]}


def test_the_message_names_the_var_the_script_line_where_pdt_looked_and_the_fix(ps_app, project):
    app = config.merged_app("ps-report")
    assert config.missing_env(app) == expected(project)
    os.environ["TENANT_ID"] = "t"
    assert config.missing_env(app) == ""


def test_pdt_run_stops_before_the_app_starts(ps_app, project, monkeypatch, capsys):
    monkeypatch.setattr(cli.subprocess, "run", lambda *a, **k: pytest.fail("the app started"))
    assert run_cli(monkeypatch, "run", "ps-report") == 1
    assert expected(project) in " ".join(capsys.readouterr().out.split())


def test_pdt_validate_reports_the_read(ps_app, project, monkeypatch, capsys):
    assert run_cli(monkeypatch, "validate") == 1
    assert f"ps-report: {expected(project)}" in " ".join(capsys.readouterr().out.split())


def test_deploy_stops_the_same_way_on_windows_and_in_the_cloud(ps_app, project, monkeypatch, capsys):
    monkeypatch.setattr(deploy, "dispatch", lambda *a, **k: pytest.fail("dispatched"))
    outputs = {}
    for provider in ("windows", "azure"):
        (project / "pdt.yml").write_text(f"platform:\n  provider: {provider}\n")
        assert deploy.deploy("ps-report", assume_yes=True) == 1
        outputs[provider] = " ".join(capsys.readouterr().out.split())
    assert outputs["windows"] == outputs["azure"]
    assert f"ps-report: {expected(project)}" in outputs["azure"]


def test_deploy_sends_the_read_var_to_the_job(ps_app, monkeypatch):
    app = config.merged_app("ps-report")
    with pytest.raises(SystemExit):
        deploy_common.gather_secrets(app)
    os.environ["TENANT_ID"] = "t"
    os.environ["NOTE"] = "n"
    assert deploy_common.gather_secrets(app) == {"TENANT_ID": "t", "NOTE": "n"}


@pytest.mark.parametrize("action", ["diff", "save"])
def test_secrets_diff_and_save_check_the_read(ps_app, project, monkeypatch, capsys, action):
    monkeypatch.setattr(deploy, "dispatch", lambda *a, **k: pytest.fail("dispatched"))
    assert deploy.secrets("ps-report", action) == 1
    assert f"ps-report: {expected(project)}" in " ".join(capsys.readouterr().out.split())


def test_the_wrapper_stops_before_any_script(ps_app, project, monkeypatch, capsys):
    monkeypatch.chdir(ps_app)
    monkeypatch.setattr(run_powershell, "ensure_pwsh", lambda: "/fake/pwsh")
    monkeypatch.setattr(run_powershell.subprocess, "run", lambda *a, **k: pytest.fail("a script ran"))
    with pytest.raises(SystemExit) as stop:
        run_powershell.main([str(ps_app)])
    assert stop.value.code == 1
    assert f"env vars missing: {expected(project)}" in " ".join(capsys.readouterr().out.split())


def test_the_scan_runs_pwsh_once_per_state_of_the_files(tmp_path, monkeypatch):
    (tmp_path / "report.ps1").write_text("Get-Date\n")
    (tmp_path / "scans").mkdir()
    monkeypatch.setenv("PDT_SCAN_DIR", str(tmp_path / "scans"))
    calls = []

    def fake_pwsh(command, **kwargs):
        calls.append(command)
        Path(command[-1]).write_text(json.dumps(FACTS))
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(powershell.pwsh, "ensure_pwsh", lambda: "/fake/pwsh")
    monkeypatch.setattr(powershell.subprocess, "run", fake_pwsh)
    assert powershell.extract(tmp_path) == FACTS
    assert powershell.extract(tmp_path) == FACTS
    assert len(calls) == 1
    (tmp_path / "report.ps1").write_text("Get-Date\nGet-Date\n")
    powershell.extract(tmp_path)
    assert len(calls) == 2


def test_the_first_scan_names_its_folder_for_the_processes_the_command_starts(monkeypatch):
    monkeypatch.delenv("PDT_SCAN_DIR", raising=False)
    folder = powershell.scan_dir()
    assert folder.is_dir()
    assert os.environ["PDT_SCAN_DIR"] == str(folder)
    assert powershell.scan_dir() == folder
