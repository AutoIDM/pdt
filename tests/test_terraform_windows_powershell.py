import json
import os
import re
import subprocess

import pytest

from pdt import terraform_windows
from pdt.deploy_windows import task_xml


PWSH = os.environ.get("PDT_POWERSHELL")
pytestmark = pytest.mark.skipif(not PWSH, reason="set PDT_POWERSHELL to run PowerShell integration tests")

XML = """<Task><RegistrationInfo><Description>Managed by pdt; report</Description></RegistrationInfo><Settings><ExecutionTimeLimit>PT24H</ExecutionTimeLimit></Settings></Task>"""


def command(action):
    return terraform_windows.configuration("pdt-report", XML)["shell_script"]["task"]["os_commands"]["windows"][action]["command"]


def run(script, tmp_path, actual=None, description="Managed by pdt; report"):
    output = tmp_path / "output.json"
    error = tmp_path / "error.txt"
    setup = "function Get-ScheduledTask { [pscustomobject]@{ Description = '" + description + "' } }; "
    if actual is None:
        setup = "function Get-ScheduledTask { $null }; "
    else:
        setup += "function Export-ScheduledTask { @'\n" + actual + "\n'@ }; "
    result = subprocess.run([PWSH, "-NoProfile", "-Command", setup + script], env=dict(os.environ, TF_SCRIPT_OUTPUT=str(output), TF_SCRIPT_ERROR=str(error)), capture_output=True, text=True)
    return result, json.loads(output.read_text()) if output.exists() else None, error.read_text() if error.exists() else ""


def test_every_generated_script_parses():
    for action in ("create", "read", "update", "delete", "plan"):
        source = command(action).replace("'", "''")
        result = subprocess.run([PWSH, "-NoProfile", "-Command", "$e=$null; [System.Management.Automation.Language.Parser]::ParseInput('" + source + "',[ref]$null,[ref]$e) | Out-Null; if($e){exit 1}"], capture_output=True, text=True)
        assert result.returncode == 0, result.stderr


def test_read_detects_absence_and_ignores_extra_defaults(tmp_path):
    result, state, error = run(command("read"), tmp_path)
    assert result.returncode == 0
    assert state["__meta"]["output_drift_detected"] is True
    actual = XML.replace("</Settings>", "<Hidden>false</Hidden></Settings>")
    result, state, error = run(command("read"), tmp_path, actual)
    assert result.returncode == 0
    assert state["__meta"]["output_drift_detected"] is False
    assert error == ""


def test_read_detects_changed_task_and_refuses_unmanaged_task(tmp_path):
    changed = XML.replace("PT24H", "PT1H")
    result, state, error = run(command("read"), tmp_path, changed)
    assert result.returncode == 0
    assert state["__meta"]["output_drift_detected"] is True
    unmanaged = XML.replace("Managed by pdt; report", "other task")
    result, state, error = run(command("read"), tmp_path, unmanaged, "other task")
    assert result.returncode == 1
    assert "not managed by PDT" in error


def test_read_compares_equivalent_durations(tmp_path):
    result, state, _error = run(command("read"), tmp_path, XML.replace("PT24H", "P1D"))

    assert result.returncode == 0, result.stderr
    assert state["__meta"]["output_drift_detected"] is False


@pytest.mark.parametrize("before,after", [
    ("IgnoreNew", "Parallel"),
    ("S-1-5-18", "S-1-5-19"),
    ("HighestAvailable", "LeastPrivilege"),
    ("DaysInterval>1", "DaysInterval>2"),
    ("run --script run.py", "run --script other.py"),
    ("<Enabled>true</Enabled>\n    <Hidden>", "<Enabled>false</Enabled>\n    <Hidden>"),
])
def test_read_detects_modelled_task_changes(tmp_path, before, after):
    app = {"name": "report", "dir": tmp_path, "schedule": "0 1 * * *", "timezone": "local"}
    _description, xml = task_xml(app, str(tmp_path / "uv.exe"))
    assert before in xml
    script = terraform_windows.configuration("pdt-report", xml)["shell_script"]["task"]["os_commands"]["windows"]["read"]["command"]

    result, state, _error = run(script, tmp_path, xml.replace(before, after))

    assert result.returncode == 0, result.stderr
    assert state["__meta"]["output_drift_detected"] is True


@pytest.mark.parametrize("action", ["create", "delete"])
@pytest.mark.parametrize("owned", [True, False])
def test_elevated_commands_preserve_payload_and_check_ownership(tmp_path, action, owned):
    name = "pdt-O'Brien"
    source = terraform_windows.configuration(name, XML)["shell_script"]["task"]["os_commands"]["windows"][action]["command"]
    elevated = re.search(r"@'\n(.*?)\n'@", source, re.DOTALL).group(1)
    output = tmp_path / "mutation.json"
    setup = r'''
function Register-ScheduledTask { param($Xml, $TaskName, $TaskPath, [switch]$Force, $ErrorAction); @{name=$TaskName; xml=$Xml} | ConvertTo-Json | Set-Content $env:PDT_MUTATION }
function Unregister-ScheduledTask { param($TaskName, $TaskPath, [switch]$Confirm, $ErrorAction); @{name=$TaskName} | ConvertTo-Json | Set-Content $env:PDT_MUTATION }
'''
    description = "Managed by pdt; report" if owned else "another task"
    setup += "function Get-ScheduledTask { [pscustomobject]@{Description='" + description + "'} };\n"

    result = subprocess.run([PWSH, "-NoProfile", "-Command", setup + elevated],
                            env=dict(os.environ, PDT_MUTATION=str(output)), capture_output=True, text=True)

    if owned:
        assert result.returncode == 0, result.stderr
        mutation = json.loads(output.read_text())
        assert mutation["name"] == name
        if action == "create":
            assert mutation["xml"] == XML
    else:
        assert result.returncode == 1
        assert not output.exists()
