import json
import os
import shutil
import subprocess

import pytest

from pdt import terraform, terraform_windows


XML = """<Task><RegistrationInfo><Description>Managed by pdt; runs report</Description></RegistrationInfo></Task>"""


def test_shell_configuration_has_full_windows_lifecycle():
    resources = terraform_windows.configuration("pdt-report", XML)
    task = resources["shell_script"]["task"]
    commands = task["os_commands"]["windows"]

    assert set(commands) == {"create", "read", "update", "delete", "plan"}
    assert task["inputs"] == {"name": "pdt-report", "xml": XML, "adopt": False}
    assert commands["create"]["interpreter"][-1] == "-Command"


def test_read_script_compares_modeled_task_fields_and_emits_utf8_json():
    script = terraform_windows.configuration("pdt-report", XML)["shell_script"]["task"]["os_commands"]["windows"]["read"]["command"]

    assert "GetElementsByTagName" in script
    assert "output_drift_detected" in script
    assert "UTF8Encoding($false)" in script
    assert "Managed by pdt;" in script
    assert "Matches $desiredDoc.DocumentElement $actualDoc.DocumentElement" in script
    assert "ExecutionTimeLimit" in script
    assert "InnerText.StartsWith" in script


def test_adoption_create_script_does_not_register_the_task():
    script = terraform_windows.configuration("pdt-report", XML, adopt=True)["shell_script"]["task"]["os_commands"]["windows"]["create"]["command"]

    assert "Register-ScheduledTask" not in script
    assert "does not exist for adoption" in script


def test_update_and_delete_elevate_only_the_task_scheduler_command():
    resources = terraform_windows.configuration("pdt-report", XML)["shell_script"]["task"]
    update = resources["os_commands"]["windows"]["update"]["command"]
    delete = resources["os_commands"]["windows"]["delete"]["command"]

    assert "Start-Process powershell.exe -Verb RunAs" in update
    assert "Register-ScheduledTask" in update
    assert "FromBase64String" in update
    assert "catch { exit 1 }" in update
    assert "Start-Process powershell.exe -Verb RunAs" in delete
    assert "Unregister-ScheduledTask" in delete
    assert "Register-ScheduledTask -Xml $xml" in update
    assert "-Confirm:$false" in delete
    assert "-Confirm:`$false" not in delete
    assert "if ($task -and -not $task.Description.StartsWith" in update


@pytest.mark.skipif(os.environ.get("PDT_RUN_TERRAFORM_PROVIDER_VALIDATE") != "1",
                    reason="set PDT_RUN_TERRAFORM_PROVIDER_VALIDATE=1 to validate the cached shell provider")
def test_windows_shell_configuration_validates_with_cached_provider(tmp_path):
    binary = shutil.which("terraform")
    assert binary
    source = ".secrets/debug/terraform-migration/schemas/shell"
    shutil.copytree(source + "/.terraform", tmp_path / ".terraform")
    shutil.copy(source + "/.terraform.lock.hcl", tmp_path / ".terraform.lock.hcl")
    configuration = terraform.configuration("shell", {}, terraform_windows.configuration("pdt-report", XML))
    (tmp_path / "main.tf.json").write_text(json.dumps(configuration))

    result = subprocess.run([binary, f"-chdir={tmp_path}", "validate", "-no-color"],
                            capture_output=True, text=True)

    assert result.returncode == 0, result.stderr or result.stdout
