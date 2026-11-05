import io
import subprocess
import urllib.error
from types import SimpleNamespace

import pytest
from botocore.exceptions import ClientError

from pdt import deploy_aws_batch, deploy_azure, deploy_google_cloud


class Read(Exception):
    pass


def aws_deploy(monkeypatch, error_code):
    module = deploy_aws_batch

    def get_secret_value(SecretId):
        raise ClientError({"Error": {"Code": error_code, "Message": "no"}}, "GetSecretValue")

    def registration_secrets(current):
        raise Read(current)

    clients = {"sts": None, "iam": None, "ec2": None, "batch": None, "scheduler": None,
               "secretsmanager": SimpleNamespace(get_secret_value=get_secret_value)}
    monkeypatch.setattr(module, "build_in_cloud", lambda *args: False)
    monkeypatch.setattr(module, "ensure_session", lambda app: None)
    monkeypatch.setattr(module, "aws_settings", lambda app, session: ("1", "us-east-1"))
    monkeypatch.setattr(module, "batch_clients", lambda session: clients)
    monkeypatch.setattr(module, "preflight", lambda *args: ("1", lambda: None))
    monkeypatch.setattr(module, "gather_secrets", lambda app: {})
    monkeypatch.setattr(module, "default_network", lambda ec2: ("vpc", []))
    monkeypatch.setattr(module, "find_security_group", lambda ec2, vpc: None)
    monkeypatch.setattr(module, "environment_security_groups", lambda found: [])
    monkeypatch.setattr(module, "describe", lambda *args: None)
    monkeypatch.setattr(module, "resource_exists", lambda *args, **kwargs: True)
    monkeypatch.setattr(module, "registration_secrets", registration_secrets)
    return module.deploy({"name": "report", "schedule": "daily", "storage": False}, True)


def test_aws_reads_a_secret_with_no_value_as_none(monkeypatch):
    with pytest.raises(Read) as read:
        aws_deploy(monkeypatch, "ResourceNotFoundException")
    assert read.value.args == (None,)


def test_aws_stops_on_a_refused_secret_read(monkeypatch):
    with pytest.raises(ClientError, match="AccessDeniedException"):
        aws_deploy(monkeypatch, "AccessDeniedException")


@pytest.mark.parametrize(("code", "stderr", "value"), [
    (0, "", "{}"),
    (1, "ERROR: NOT_FOUND: Secret Version [latest] not found or has no versions.", None),
])
def test_google_cloud_reads_a_missing_secret_as_none(monkeypatch, code, stderr, value):
    monkeypatch.setattr(deploy_google_cloud.subprocess, "run", lambda *args, **kwargs:
                        subprocess.CompletedProcess(args, code, "{}" if not code else "", stderr))
    assert deploy_google_cloud.secret_value("p", "pdt-report-env") == value


def test_google_cloud_stops_on_a_refused_secret_read_and_names_the_permission(monkeypatch,
                                                                              capsys):
    monkeypatch.setattr(deploy_google_cloud.subprocess, "run", lambda *args, **kwargs:
                        subprocess.CompletedProcess(args, 1, "", "ERROR: PERMISSION_DENIED"))
    with pytest.raises(SystemExit):
        deploy_google_cloud.secret_value("p", "pdt-report-env")
    out = " ".join(capsys.readouterr().out.split())
    assert "secretmanager.versions.access" in out


SETTINGS = {"vault": "pdtvault"}


def azure_answers(monkeypatch, statuses):
    def open_url(request, timeout):
        status = statuses.pop(0)
        if status == 200:
            return io.BytesIO(b'{"value": "{\\"A\\": \\"1\\"}"}')
        raise urllib.error.HTTPError(request.full_url, status, "no", {}, io.BytesIO(b"{}"))

    monkeypatch.setattr(deploy_azure, "access_token", lambda resource="": "t")
    monkeypatch.setattr(deploy_azure.READ_OPENER, "open", open_url)
    monkeypatch.setattr(deploy_azure.time, "sleep", lambda seconds: None)


def test_azure_reads_the_secret_once_the_role_reaches_the_vault(monkeypatch):
    azure_answers(monkeypatch, [403, 403, 200])
    assert deploy_azure.granted_secret_value(SETTINGS, "pdt-report") == '{"A": "1"}'


def test_azure_reads_a_missing_secret_as_none(monkeypatch):
    azure_answers(monkeypatch, [404])
    assert deploy_azure.granted_secret_value(SETTINGS, "pdt-report") is None


def test_azure_stops_when_the_secret_stays_refused_and_names_the_role(monkeypatch, capsys):
    azure_answers(monkeypatch, [403, 403, 403, 403])
    with pytest.raises(SystemExit):
        deploy_azure.granted_secret_value(SETTINGS, "pdt-report")
    out = " ".join(capsys.readouterr().out.split())
    assert "needs the role Key Vault Secrets Officer on Key Vault pdtvault" in out


def test_azure_reads_a_refused_secret_again_without_managed_services(monkeypatch, tmp_path):
    from test_azure_shared_locks import APP, EXISTING, fake_deploy

    from pdt import deploy_azure_container_apps
    module = deploy_azure_container_apps
    fake_deploy(monkeypatch, EXISTING)
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("PDT_PROJECT", raising=False)
    written = []
    deployed = ('{"A": "0", "PDT_API_URL": "u", "PDT_CUSTOMER_ID": "cus_7a1c09e4b2", '
                '"PDT_INSTALLATION_ID": "i", "PDT_INSTALLATION_KEY": "k"}')
    monkeypatch.setattr(module, "gather_secrets", lambda app: {"A": "1"})
    monkeypatch.setattr(module, "secret_state", lambda *args: (True, None))
    monkeypatch.setattr(module, "granted_secret_value", lambda settings, sid: deployed)
    monkeypatch.setattr(module, "ensure_secret",
                        lambda settings, sid, values, *args: written.append(values))
    assert module.deploy(APP, True) == 0
    assert written[0]["PDT_INSTALLATION_ID"] == "i"
    assert written[0]["A"] == "1"
