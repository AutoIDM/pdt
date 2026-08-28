from pathlib import Path, PureWindowsPath
import sys
import types

import pytest

from pdt.deploy_windows import destroy, deploy
from pdt.windows_remote import (
    AdministratorCredentials, RemoteWindowsDeployment, RemoteWindowsState,
    RemoteWindowsHost, WindowsDeployError, _ACTIVATE_RELEASE_SCRIPT,
    _INSPECT_SCRIPT, _PREPARE_INCOMING_SCRIPT, _REMOVE_SCRIPT,
    _REMOVE_STALE_SCRIPT, _UV_SCRIPT, _task_xml, deployment_plan, destroy_plan,
    destroy_remote,
)


def desired():
    return RemoteWindowsDeployment(
        host="jobs-01", app_name="report", task_name="pdt-report",
        app_root=PureWindowsPath(r"C:\ProgramData\pdt\apps\pdt-report"),
        release_root=PureWindowsPath(r"C:\ProgramData\pdt\apps\pdt-report\releases\abc"),
        uv_path=PureWindowsPath(r"C:\ProgramData\pdt\bin\uv.exe"),
        artifact_digest="abc", schedule_description="daily at 01:00",
        trigger_xml="<CalendarTrigger/>")


def state(**changes):
    values = dict(task="absent", app_directory="absent", active_release=None,
                  running_release=None, files_match=False, secrets_match=False,
                  uv_matches=False, other_managed_apps=0)
    values.update(changes)
    return RemoteWindowsState(**values)


def test_remote_plan_names_host_and_resources():
    actions = deployment_plan(desired(), state())
    assert actions[0] == "Windows host: jobs-01"
    assert "install uv 0.12.4" in actions[1]
    assert any("pdt-report" in action for action in actions)


def test_remote_xml_uses_release_paths_not_controller_paths():
    xml = _task_xml(desired())
    assert r"C:\ProgramData\pdt\apps\pdt-report\releases\abc\run.ps1" in xml
    assert "runs as SYSTEM" not in xml
    assert "S-1-5-18" in xml
    assert r"<WorkingDirectory>C:\ProgramData\pdt\apps\pdt-report\releases\abc</WorkingDirectory>" in xml


def test_credentials_hide_the_password():
    assert "secret" not in repr(AdministratorCredentials("admin", "secret"))


def test_unmanaged_task_refuses_deploy(monkeypatch, capsys, tmp_path):
    app = {"name": "report", "platform": {"host": "jobs-01"}}
    monkeypatch.setattr("pdt.windows_remote._credentials", lambda host: AdministratorCredentials("a", "b"))

    class Remote:
        def desired(self, host, app, stage):
            return desired()

        def inspect(self, desired, secret_json):
            return state(task="unmanaged")

        def close(self):
            pass

    monkeypatch.setattr("pdt.windows_remote.RemoteWindowsHost.connect", lambda host, credentials: Remote())
    monkeypatch.setattr("pdt.windows_remote.gather_secrets", lambda app: {})
    monkeypatch.setattr("pdt.windows_remote.stage_build_context", lambda app: tmp_path)
    assert deploy(app, True) == 1
    assert "not managed by PDT" in capsys.readouterr().out


def test_destroy_plan_stops_task_and_removes_shared_tools_after_last_app():
    actions = destroy_plan(desired(), state(task="managed", app_directory="managed"))
    assert "stop and delete Windows scheduled task pdt-report" in actions[1]
    assert actions[-1] == r"delete shared directory C:\ProgramData\pdt\bin"


def test_windows_without_a_host_uses_local_deploy(monkeypatch):
    app = {"platform": {}}
    monkeypatch.setattr("pdt.deploy_windows._deploy_local", lambda app, yes: 12)
    assert deploy(app, True) == 12


def test_windows_with_a_host_uses_remote_deploy(monkeypatch):
    app = {"platform": {"host": "jobs-01"}}
    monkeypatch.setattr("pdt.windows_remote.deploy_remote", lambda app, host, yes: 13)
    assert deploy(app, True) == 13


def test_run_uses_the_powershell_parameter_api(monkeypatch):
    calls = []

    class Powershell:
        def __init__(self, pool):
            self.streams = types.SimpleNamespace(error=[])

        def add_script(self, script):
            calls.append(("script", script))

        def add_parameters(self, parameters):
            calls.append(("parameters", parameters))

        def invoke(self):
            return ["ok"]

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

    module = types.ModuleType("pypsrp.powershell")
    module.PowerShell = Powershell
    monkeypatch.setitem(sys.modules, "pypsrp.powershell", module)
    host = RemoteWindowsHost(types.SimpleNamespace(), object())
    assert host._run("param($secret_json) $secret_json", secret_json="do-not-inline") == ["ok"]
    assert calls == [
        ("script", "param($secret_json) $secret_json"),
        ("parameters", {"secret_json": "do-not-inline"}),
    ]


def test_stage_is_removed_after_inspect_failure(monkeypatch, tmp_path):
    stage = tmp_path / "stage"
    stage.mkdir()
    app = {"name": "report", "platform": {"host": "jobs-01"}}

    class Remote:
        def desired(self, host, app, stage):
            return desired()

        def inspect(self, desired, secret_json):
            raise WindowsDeployError("inspect failed")

        def close(self):
            pass

    monkeypatch.setattr("pdt.windows_remote._credentials", lambda host: AdministratorCredentials("a", "b"))
    monkeypatch.setattr("pdt.windows_remote.RemoteWindowsHost.connect", lambda host, credentials: Remote())
    monkeypatch.setattr("pdt.windows_remote.gather_secrets", lambda app: {})
    monkeypatch.setattr("pdt.windows_remote.stage_build_context", lambda app: stage)
    assert deploy(app, True) == 1
    assert not stage.exists()


def test_destroy_does_not_require_a_schedule(monkeypatch):
    app = {"name": "report", "platform": {"host": "jobs-01"}}

    class Remote:
        def inspect(self, desired, secret_json):
            return state()

        def close(self):
            pass

    monkeypatch.setattr("pdt.windows_remote._credentials", lambda host: AdministratorCredentials("a", "b"))
    monkeypatch.setattr("pdt.windows_remote.RemoteWindowsHost.connect", lambda host, credentials: Remote())
    assert destroy_remote(app, "jobs-01", True) == 0


def test_task_ownership_binds_app_and_root():
    assert 'app=$app_name; root=$app_root; release=' in _INSPECT_SCRIPT


def test_unmanaged_root_is_checked_before_writing_markers():
    assert "PDT root ownership marker does not match" in _PREPARE_INCOMING_SCRIPT
    assert "PDT root ownership marker is missing" in _PREPARE_INCOMING_SCRIPT
    assert _PREPARE_INCOMING_SCRIPT.index("PDT root ownership marker") < _PREPARE_INCOMING_SCRIPT.index("Set-Content $rootMarker")


def test_wrapper_uses_the_app_directory_and_manifest_has_no_secret_digest():
    assert "Set-Location (Join-Path $release 'APP_NAME')" in _ACTIVATE_RELEASE_SCRIPT
    manifest = next(line for line in _ACTIVATE_RELEASE_SCRIPT.splitlines()
                    if "artifact_digest" in line)
    assert "secret_digest" not in manifest
    assert "exit $LASTEXITCODE" in _ACTIVATE_RELEASE_SCRIPT


def test_remove_handles_task_or_directory_on_its_own():
    assert "if ($null -ne $task)" in _REMOVE_SCRIPT
    assert "if (Test-Path $app_root)" in _REMOVE_SCRIPT


def test_stale_cleanup_reads_current_running_release():
    assert "Get-CimInstance Win32_Process" in _REMOVE_STALE_SCRIPT


def test_inspection_uses_windows_powershell_digest_apis():
    assert "SHA256Managed" in _INSPECT_SCRIPT
    assert "HashData" not in _INSPECT_SCRIPT
    assert "FromHexString" not in _INSPECT_SCRIPT
    assert "CryptographicOperations" not in _INSPECT_SCRIPT


def test_corrupt_active_or_running_release_refuses_replacement(tmp_path):
    host = RemoteWindowsHost(types.SimpleNamespace(), object())
    active = state(active_release=desired().release_root)
    with pytest.raises(WindowsDeployError, match="active or running"):
        host.reconcile(desired(), active, tmp_path, "{}")
    running = state(running_release=desired().release_root)
    with pytest.raises(WindowsDeployError, match="active or running"):
        host.reconcile(desired(), running, tmp_path, "{}")


def test_uv_script_rejects_unsupported_architecture_before_download():
    assert _UV_SCRIPT.index("PROCESSOR_ARCHITECTURE") < _UV_SCRIPT.index("Invoke-WebRequest")
    assert "PDT supports only x86_64 Windows hosts" in _UV_SCRIPT


def test_prepare_runs_before_uv_on_a_first_deploy(monkeypatch, tmp_path):
    calls = []
    host = RemoteWindowsHost(types.SimpleNamespace(), object())
    monkeypatch.setattr(host, "_run", lambda script, **parameters: calls.append(script))
    monkeypatch.setattr(host, "_copy", lambda source, destination: None)
    host.reconcile(desired(), state(), tmp_path, "{}")
    assert calls.index(_PREPARE_INCOMING_SCRIPT) < calls.index(_UV_SCRIPT)


def test_remove_uses_current_managed_app_count_and_a_bounded_wait():
    assert "$other" in _REMOVE_SCRIPT
    assert "did not stop within 60 seconds" in _REMOVE_SCRIPT
    assert "PDT root ownership marker is missing" in _REMOVE_SCRIPT
    assert "Remove-Item $root -Recurse" not in _REMOVE_SCRIPT
    assert "-not @(Get-ChildItem $root -Force).Count" in _REMOVE_SCRIPT
