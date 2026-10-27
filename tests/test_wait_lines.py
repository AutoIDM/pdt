import threading
import time
from types import SimpleNamespace

import pytest

from conftest import fake_azure_reads, plain
from pdt import (console, deploy_aws, deploy_aws_batch, deploy_azure, deploy_azure_container_apps,
                 deploy_google_cloud)

SUBSCRIPTION = "11111111-1111-1111-1111-111111111111"
SHARED = deploy_azure.Environment("pdt-shared", "pdt-eastus2", True)
MANAGED = {"tags": {"managed-by": "pdt"}}
SETTINGS = {
    "subscription": SUBSCRIPTION, "resource_group": "pdt", "region": "eastus2",
    "registry": "pdtregistry", "environment": SHARED, "identity": "pdt-runner",
    "workspace": "pdt-logs", "vault": "pdt-vault", "suffix": "abc1234def", "token": "t",
}


@pytest.fixture(autouse=True)
def fresh_once(monkeypatch):
    monkeypatch.setattr(console, "_warned", set())


def destroy_with(monkeypatch, answers: dict) -> dict:
    seen = {"active": 0, "most": 0, "az_threads": set(), "plans": []}
    lock = threading.Lock()

    def read(*args):
        with lock:
            seen["active"] += 1
            seen["most"] = max(seen["most"], seen["active"])
        time.sleep(0.05)
        with lock:
            seen["active"] -= 1
        return answers.get(args[:3], answers.get(args[:2]))

    def az_json(*args):
        seen["az_threads"].add(threading.current_thread().name)
        return None

    module = deploy_azure_container_apps
    monkeypatch.setattr(module, "preflight", lambda app, requested: dict(SETTINGS))
    monkeypatch.setattr(module, "azure_settings", lambda app: {})
    fake_azure_reads(monkeypatch, read)
    monkeypatch.setattr(module, "az_json", az_json)
    monkeypatch.setattr(module, "confirm",
                        lambda actions, *args: seen["plans"].append(plain(actions)) and False)
    module.destroy({"name": "report", "storage": False}, True)
    return seen


def test_destroy_reads_its_resources_at_the_same_time(monkeypatch, capsys):
    seen = destroy_with(monkeypatch, {})
    assert seen["most"] >= 5
    out, err = capsys.readouterr()
    assert "Checking current state in Azure subscription" in out
    assert "reading: resources" in err


def test_destroy_in_a_shared_group_reads_the_image_with_the_azure_cli_on_the_main_thread(
        monkeypatch):
    seen = destroy_with(monkeypatch, {
        ("group", "show"): MANAGED,
        ("resource", "list"): [{"tags": {"managed-by": "pdt", "pdt-app": "other"}}],
        ("acr", "show"): MANAGED,
    })
    assert seen["az_threads"] == {threading.main_thread().name}


def test_destroy_of_the_last_app_reads_the_shared_environment_and_no_image(monkeypatch):
    seen = destroy_with(monkeypatch, {
        ("group", "show"): MANAGED,
        ("resource", "list"): [{"tags": {"managed-by": "pdt", "pdt-app": "report"}}],
        ("containerapp", "env", "show"): MANAGED,
        ("containerapp", "job", "list"): [],
    })
    assert seen["az_threads"] == set()
    assert "delete Container Apps environment pdt-shared/pdt-eastus2 (no other job uses it)" in (
        seen["plans"][0])


def test_the_azure_sign_in_check_shows_one_wait_line(monkeypatch, capsys):
    account = {"id": SUBSCRIPTION, "name": "Azure", "state": "Enabled", "user": {"type": "user"}}
    monkeypatch.setattr(deploy_azure, "can_prompt", lambda interactive: False)
    monkeypatch.setattr(deploy_azure, "az_json", lambda *args: account)
    monkeypatch.setattr(deploy_azure, "access_token", lambda resource="": "x.e30.y")
    monkeypatch.setenv("PDT_AZURE_DEPLOYER_OBJECT_ID", "deployer")
    deploy_azure.preflight({"platform": {}}, {"subscription": SUBSCRIPTION, "resource_group": "pdt"})
    assert capsys.readouterr().err.splitlines() == ["checking: Azure sign-in"]


def test_the_aws_sign_in_check_shows_a_wait_line(capsys):
    sts = SimpleNamespace(get_caller_identity=lambda: {
        "Account": "123456789012", "Arn": "arn:aws:iam::123456789012:root"})
    deploy_aws.preflight(sts, None, "123456789012", "us-east-1", [])
    assert capsys.readouterr().err.splitlines() == ["checking: AWS sign-in"]


def test_the_google_cloud_sign_in_check_shows_a_wait_line(monkeypatch, capsys):
    monkeypatch.setattr(deploy_google_cloud, "have_credentials", lambda: True)
    deploy_google_cloud.ensure_credentials()
    assert capsys.readouterr().err.splitlines() == ["checking: Google Cloud sign-in"]


def test_azure_secrets_reads_the_job_and_the_secret_under_a_wait_line(monkeypatch, capsys):
    module = deploy_azure_container_apps
    monkeypatch.setattr(module, "preflight", lambda app, requested: dict(SETTINGS))
    monkeypatch.setattr(module, "azure_settings", lambda app: {})
    fake_azure_reads(monkeypatch, lambda *args: None)
    monkeypatch.setattr(module, "run_secrets", lambda action, app, current, *args: current)
    assert module.secrets({"name": "report"}, "diff", True) is None
    assert "reading: resources" in capsys.readouterr().err


def test_aws_secrets_reads_the_secret_under_a_wait_line(monkeypatch, capsys):
    module = deploy_aws_batch
    client = SimpleNamespace(get_secret_value=lambda SecretId: {"SecretString": "{}"})
    monkeypatch.setattr(module, "ensure_session", lambda app: None)
    monkeypatch.setattr(module, "aws_settings", lambda app, session: ("1", "us-east-1"))
    monkeypatch.setattr(module, "batch_clients", lambda session: {
        "sts": None, "iam": None, "secretsmanager": client})
    monkeypatch.setattr(module, "preflight", lambda *args: ("1", lambda: None))
    monkeypatch.setattr(module, "resource_exists", lambda *args, **kwargs: True)
    monkeypatch.setattr(module, "run_secrets", lambda action, app, current, *args: current)
    assert module.secrets({"name": "report"}, "diff", True) == "{}"
    assert "reading: resources" in capsys.readouterr().err


def test_google_cloud_secrets_reads_the_secret_under_a_wait_line(monkeypatch, capsys):
    module = deploy_google_cloud
    monkeypatch.setattr(module, "project_region", lambda app: ("p", "us-east1"))
    monkeypatch.setattr(module, "preflight", lambda app, project, assume_yes: project)
    monkeypatch.setattr(module, "read_json_or_none", lambda *args: {"labels": {"managed-by": "pdt"}})
    monkeypatch.setattr(module, "secret_value", lambda project, sid: "{}")
    monkeypatch.setattr(module, "run_secrets", lambda action, app, current, *args: current)
    assert module.secrets({"name": "report"}, "diff", True) == "{}"
    assert "reading: resources" in capsys.readouterr().err
