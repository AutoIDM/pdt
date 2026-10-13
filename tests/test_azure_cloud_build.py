"""The Azure image build with an ACR Tasks quick build, and with Docker on this computer."""

import subprocess
import sys
from pathlib import Path

import pytest
from rich.text import Text

from conftest import add_app, plain
from pdt import deploy_azure_container_apps, deploy_common
from test_azure_shared_locks import EXISTING, fake_deploy

SETTINGS = {
    "subscription": "11111111-1111-1111-1111-111111111111", "resource_group": "pdt",
    "registry": "pdtregistry", "deployer_object_id": "d", "deployer_principal_type": "User",
}
ACR_ID = ("/subscriptions/11111111-1111-1111-1111-111111111111/resourceGroups/pdt"
          "/providers/Microsoft.ContainerRegistry/registries/pdtregistry")


def said(capsys) -> str:
    return Text.from_markup(capsys.readouterr().out).plain.replace("\n", " ")


def app_in(project) -> dict:
    folder = add_app(project, "report")
    (folder / ".env").write_text("TOKEN=secret\n")
    (folder / "id_rsa").write_text("key\n")
    (folder / "data.csv").write_text("a,b\n")
    return {"name": "report", "dir": folder, "platform": {"provider": "azure"}}


def fake_az(monkeypatch, returncode=0, stderr=""):
    """`az` as the build sees it, and a subprocess.run that no build may reach.

    Returns the az calls, each with the staged files `az acr build` was given and the
    error mode of stdout while it ran.
    """
    calls = []

    def az(*args):
        call = {"args": args, "stdout_errors": sys.stdout.errors}
        if args[:2] == ("acr", "build"):
            stage = Path(args[-1])
            call["files"] = sorted(str(path.relative_to(stage)) for path in stage.rglob("*"))
        calls.append(call)
        return subprocess.CompletedProcess(args, returncode, "", stderr)

    def no_process(command, *args, **kwargs):
        raise AssertionError(f"ran {command}")

    monkeypatch.setattr(deploy_azure_container_apps, "az", az)
    monkeypatch.setattr(deploy_azure_container_apps, "run_quiet",
                        lambda *args, **kwargs: calls.append({"args": args}) or "")
    monkeypatch.setattr(deploy_common.subprocess, "run", no_process)
    return calls


def test_the_cloud_build_sends_the_staged_context_to_acr_tasks(project, monkeypatch):
    calls = fake_az(monkeypatch)
    deploy_azure_container_apps.build_image(app_in(project), SETTINGS, "report", True)
    assert len(calls) == 1
    args = calls[0]["args"]
    assert args[:2] == ("acr", "build")
    assert args[args.index("--registry") + 1] == "pdtregistry"
    assert args[args.index("--resource-group") + 1] == "pdt"
    assert args[args.index("--image") + 1] == "report:latest"
    assert args[args.index("--platform") + 1] == "linux/amd64"
    assert "--no-format" in args
    assert not Path(args[-1]).exists()


def test_the_cloud_build_context_holds_no_secret_files(project, monkeypatch):
    calls = fake_az(monkeypatch)
    deploy_azure_container_apps.build_image(app_in(project), SETTINGS, "report", True)
    files = calls[0]["files"]
    assert {"Dockerfile", "pdt.yml", "report", "report/run.py", "report/data.csv"} <= set(files)
    assert not [name for name in files if name.endswith((".env", "id_rsa"))]


def test_the_build_log_cannot_stop_on_a_character_the_console_cannot_encode(project, monkeypatch):
    before = sys.stdout.errors
    calls = fake_az(monkeypatch)
    deploy_azure_container_apps.build_image(app_in(project), SETTINGS, "report", True)
    assert calls[0]["stdout_errors"] == "replace"
    assert sys.stdout.errors == before


def test_a_failed_cloud_build_fails_once_and_shows_the_error(project, monkeypatch, capsys):
    calls = fake_az(monkeypatch, 1, "ERROR: Run failed")
    with pytest.raises(SystemExit):
        deploy_azure_container_apps.build_image(app_in(project), SETTINGS, "report", True)
    assert [call["args"][:2] for call in calls] == [("acr", "build")]
    out = said(capsys)
    assert "ERROR: Run failed" in out
    assert "pdt az acr build failed; the build log and the error above say why" in out


def test_paused_acr_tasks_get_a_plain_message(project, monkeypatch, capsys):
    error = ("ERROR: (TasksOperationsNotAllowed) ACR Tasks requests for the registry pdtregistry "
             "are not permitted. Please file an Azure support request at "
             "http://aka.ms/azuresupport for assistance.")
    calls = fake_az(monkeypatch, 1, error)
    with pytest.raises(SystemExit):
        deploy_azure_container_apps.build_image(app_in(project), SETTINGS, "report", True)
    assert len(calls) == 1
    out = said(capsys)
    assert "Azure pauses ACR Tasks for subscriptions on free credits" in out
    assert "Start Docker on this computer and run the same command again" in out
    assert "pay-as-you-go" in out
    assert "support request" not in out


def test_a_missing_acr_tasks_permission_names_the_role(project, monkeypatch, capsys):
    error = ("ERROR: Failed to get a SAS URL to upload context. Error: (AuthorizationFailed) The "
             "client 'me@example.com' with object id 'd' does not have authorization to perform "
             "action 'Microsoft.ContainerRegistry/registries/listBuildSourceUploadUrl/action' "
             f"over scope '{ACR_ID}' or the scope is invalid.")
    calls = fake_az(monkeypatch, 1, error)
    with pytest.raises(SystemExit):
        deploy_azure_container_apps.build_image(app_in(project), SETTINGS, "report", True)
    assert len(calls) == 1
    out = said(capsys)
    assert "Microsoft.ContainerRegistry/registries/listBuildSourceUploadUrl/action" in out
    assert ('pdt az role assignment create --assignee-object-id d --assignee-principal-type '
            f'User --role "Container Registry Tasks Contributor" --scope {ACR_ID}') in out


def test_the_local_build_never_runs_acr_tasks(project, monkeypatch):
    calls = fake_az(monkeypatch)
    commands = []
    monkeypatch.setattr(deploy_azure_container_apps, "run_build",
                        lambda command: commands.append(command))
    monkeypatch.setattr(deploy_azure_container_apps, "ssh_build_args", lambda: [])
    deploy_azure_container_apps.build_image(app_in(project), SETTINGS, "report", False)
    assert [call["args"] for call in calls] == [("acr", "login", "--name", "pdtregistry")]
    image = "pdtregistry.azurecr.io/report:latest"
    assert commands[0][:5] == ["docker", "build", "--platform", "linux/amd64", "-t"]
    assert commands[1] == ["docker", "push", image]


@pytest.mark.parametrize("cloud, where", [
    (True, "in the cloud with ACR Tasks"), (False, "with Docker on this computer")])
def test_the_plan_names_the_build_place_and_the_build_follows_it(monkeypatch, tmp_path, cloud,
                                                                 where):
    events, plans = fake_deploy(monkeypatch, EXISTING)
    builds = []
    module = deploy_azure_container_apps
    monkeypatch.setattr(module, "build_in_cloud", lambda app, builder, image_platform: cloud)
    monkeypatch.setattr(module, "image_action", deploy_common.image_action)
    monkeypatch.setattr(module, "build_image", lambda *args: builds.append(args))
    app = {"name": "report", "dir": tmp_path, "schedule": "0 0 * * *", "storage": False,
           "platform": {}, "pause": False}
    assert module.deploy(app, True) == 0
    assert f"build and push image pdtregistry.azurecr.io/report:latest {where}" in plain(plans[0])
    assert builds[0][2:] == ("report", cloud)
