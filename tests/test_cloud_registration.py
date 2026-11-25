import base64
import io
import json

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import load_pem_private_key
from test_autoidm import backend, folder, registered  # noqa: F401

from pdt import deploy_common, managed_services
from pdt.utils import autoidm

CUSTOMER = "cus_7a1c09e4b2"
REGISTRATION = {autoidm.ID_ENV: "ins_0000000007", autoidm.KEY_ENV: "-----PEM-----",
                autoidm.URL_ENV: "https://api.example", autoidm.CUSTOMER_ENV: CUSTOMER}
AWS = {"provider": "aws", "account": "123456789012", "region": "us-east-2"}


@pytest.fixture
def warned(monkeypatch):
    monkeypatch.setenv(managed_services.WARNED_ENV, "")


def app(tmp_path):
    return {"name": "demo", "dir": tmp_path, "platform": {"provider": "aws"}}


def managed(project):
    (project / "pdt.yml").write_text(f"platform:\n  provider: aws\nmanaged_services:\n"
                                     f"  customer: {CUSTOMER}\n")


def test_diff_and_save_leave_the_registration_out_and_save_keeps_it(project, monkeypatch,
                                                                    capsys):
    monkeypatch.setattr(deploy_common, "gather_secrets", lambda app: {"A": "new"})
    current = json.dumps({"A": "old", **REGISTRATION})
    written = []
    assert deploy_common.run_secrets("diff", app(project), current, written.append, True) == 0
    out = capsys.readouterr().out
    assert "updated" in out and "PDT_INSTALLATION" not in out and "PDT_API_URL" not in out
    assert deploy_common.run_secrets("save", app(project), current, written.append, True) == 0
    assert written == [{"A": "new", **REGISTRATION}]


def test_save_says_unchanged_when_only_the_registration_differs(project, monkeypatch, capsys):
    monkeypatch.setattr(deploy_common, "gather_secrets", lambda app: {"A": "1"})
    written = []
    deploy_common.run_secrets("save", app(project), json.dumps({"A": "1", **REGISTRATION}),
                              written.append, True)
    assert written == []
    assert "already match" in capsys.readouterr().out


def test_get_never_copies_the_registration_into_an_env_file(tmp_path, monkeypatch):
    monkeypatch.setattr(deploy_common, "can_prompt", lambda interactive: False)
    deploy_common.run_secrets("get", app(tmp_path), json.dumps({"A": "1", **REGISTRATION}),
                              None, True)
    assert (tmp_path / ".env.aws").read_text() == "A=1\n"


def test_set_keeps_the_registration_and_refuses_its_names(project, monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO("v\n"))
    written = []
    current = json.dumps({"A": "1", **REGISTRATION})
    assert deploy_common.run_secrets("set", app(project), current, written.append, True,
                                     "B") == 0
    assert written == [{"A": "1", "B": "v", **REGISTRATION}]
    with pytest.raises(SystemExit):
        deploy_common.run_secrets("set", app(project), current, written.append, True,
                                  autoidm.ID_ENV)
    assert "pdt keeps PDT_INSTALLATION_ID in the secret itself" in capsys.readouterr().out
    assert len(written) == 1


def test_a_deploy_keeps_the_registration_the_secret_holds(project, folder, warned, monkeypatch):
    managed(project)
    monkeypatch.setenv("CI", "true")
    current = json.dumps({"A": "1", **REGISTRATION})
    assert deploy_common.registration_secrets(current) == REGISTRATION
    assert deploy_common.make_registration({"A": "1", **REGISTRATION}, AWS) == {
        "A": "1", **REGISTRATION}


def test_a_deploy_replaces_a_registration_of_another_customer(project, backend, warned, capsys):
    url, seen, answers = backend
    managed(project)
    registered(url)
    other = {**REGISTRATION, autoidm.CUSTOMER_ENV: "cus_0000000002"}
    values = {"A": "1", **deploy_common.registration_secrets(json.dumps({"A": "1", **other}))}
    assert values == {"A": "1", **dict.fromkeys(autoidm.SECRET_NAMES, "")}
    assert "ins_0000000007 of another customer" in " ".join(capsys.readouterr().out.split())
    answers.append((200, {"installation_id": "ins_0000000008"}))
    made = deploy_common.make_registration(values, AWS)
    assert (made[autoidm.ID_ENV], made[autoidm.CUSTOMER_ENV]) == ("ins_0000000008", CUSTOMER)
    assert seen[0]["path"] == "/v1/installations"


def test_in_ci_a_registration_of_another_customer_is_dropped(project, folder, warned,
                                                              monkeypatch):
    managed(project)
    monkeypatch.setenv("CI", "true")
    other = {**REGISTRATION, autoidm.CUSTOMER_ENV: "cus_0000000002"}
    assert deploy_common.registration_secrets(json.dumps({"A": "1", **other})) == {}
    assert deploy_common.make_registration({"A": "1"}, AWS) == {"A": "1"}


def test_an_installation_answer_without_an_id_is_a_warning(project, backend, warned, capsys):
    url, _seen, answers = backend
    managed(project)
    registered(url)
    answers.append((200, {"customer_id": CUSTOMER}))
    assert deploy_common.make_registration(
        {"A": "1", **dict.fromkeys(autoidm.SECRET_NAMES, "")}, AWS) == {"A": "1"}
    assert "/v1/installations without installation_id" in " ".join(
        capsys.readouterr().out.split())


def test_a_deploy_without_managed_services_or_in_ci_adds_nothing(project, backend, warned,
                                                                  monkeypatch):
    url, seen, _answers = backend
    registered(url)
    assert deploy_common.registration_secrets(None) == {}
    managed(project)
    monkeypatch.setenv("CI", "true")
    assert deploy_common.registration_secrets(None) == {}
    assert seen == []


def test_an_unregistered_deploying_computer_warns_once_and_adds_nothing(project, folder, warned,
                                                                        capsys):
    managed(project)
    assert deploy_common.registration_secrets(None) == {}
    assert deploy_common.make_registration({"A": "1"}, AWS) == {"A": "1"}
    assert "not registered to AutoIDM" in capsys.readouterr().out


def test_a_deploy_makes_a_cloud_registration_signed_by_this_computer(project, backend, warned,
                                                                     capsys):
    url, seen, answers = backend
    managed(project)
    registered(url)
    values = {"A": "1", **deploy_common.registration_secrets(None)}
    assert values == {"A": "1", **dict.fromkeys(autoidm.SECRET_NAMES, "")}
    assert seen == []
    answers.append((200, {"installation_id": "ins_0000000008", "customer_id": CUSTOMER}))
    made = deploy_common.make_registration(values, AWS)
    body = seen[0]["body"]
    assert seen[0]["path"] == "/v1/installations"
    assert body["version"] == 1 and body["hostname"] is None
    assert body["place"] == "aws:123456789012:us-east-2"
    assert (made["A"], made[autoidm.ID_ENV], made[autoidm.URL_ENV],
            made[autoidm.CUSTOMER_ENV]) == ("1", "ins_0000000008", url, CUSTOMER)
    key = load_pem_private_key(made[autoidm.KEY_ENV].encode(), password=None)
    assert autoidm.public_key(key) == body["public_key"]
    assert "made AutoIDM cloud registration ins_0000000008" in capsys.readouterr().out
    assert sorted(path.suffix for path in autoidm.folder().iterdir()) == [".json", ".key"]


def test_a_runner_deploy_adds_a_registration_made_with_the_run_token(project, backend, warned,
                                                                       monkeypatch):
    url, seen, answers = backend
    managed(project)
    monkeypatch.setenv(autoidm.RUNNER_ENV, json.dumps(
        {"api_url": url, "command_id": "cmd_1", "run_token": "run-token"}))
    answers.append((200, {"installation_id": "ins_0000000009", "customer_id": CUSTOMER}))
    written = []

    assert deploy_common.run_secrets(deploy_common.REGISTER_ACTION, app(project),
                                     json.dumps({"A": "1"}), written.append, True) == 0

    assert seen[0]["path"] == "/v1/runner/installations"
    assert seen[0]["authorization"] == "Bearer run-token"
    assert seen[0]["body"]["command_id"] == "cmd_1"
    assert written[0]["A"] == "1"
    assert (written[0][autoidm.ID_ENV], written[0][autoidm.URL_ENV]) == ("ins_0000000009", url)
    deploy_common.run_secrets(deploy_common.REGISTER_ACTION, app(project),
                              json.dumps(written[0]), written.append, True)
    assert len(seen) == 1 and len(written) == 1


def test_a_refused_cloud_registration_is_a_warning_and_the_deploy_goes_on(project, backend,
                                                                          warned, capsys):
    url, _seen, answers = backend
    managed(project)
    registered(url)
    answers.append((401, {"error": "unauthorized"}))
    assert deploy_common.make_registration(
        {"A": "1", **dict.fromkeys(autoidm.SECRET_NAMES, "")}, AWS) == {"A": "1"}
    out = " ".join(capsys.readouterr().out.split())
    assert "AutoIDM did not make a cloud registration for aws:123456789012:us-east-2" in out
    assert f"AutoIDM refused the registration in {autoidm.files(CUSTOMER)[0]}" in out


@pytest.mark.parametrize(("platform", "place"), [
    (AWS, "aws:123456789012:us-east-2"),
    ({"provider": "azure", "subscription": "0f1e2d3c-aaaa-bbbb-cccc-123456789abc",
      "region": "eastus", "resource_group": "pdt"},
     "azure:0f1e2d3c-aaaa-bbbb-cccc-123456789abc:eastus"),
    ({"provider": "google-cloud", "project": "example-jobs", "region": "us-central1"},
     "google-cloud:example-jobs:us-central1"),
    ({"provider": "windows"}, None),
])
def test_the_place_names_the_provider_the_account_and_the_region(platform, place):
    assert managed_services.place(platform) == place


def cloud_secret(url: str) -> str:
    key = Ed25519PrivateKey.generate()
    return json.dumps({"A": "1", autoidm.ID_ENV: "ins_0000000007",
                       autoidm.KEY_ENV: autoidm.private_pem(key).decode(),
                       autoidm.URL_ENV: url, autoidm.CUSTOMER_ENV: CUSTOMER})


def test_a_destroy_revokes_the_cloud_registration_the_secret_holds(backend, capsys):
    url, seen, _answers = backend
    deploy_common.revoke_registration(cloud_secret(url))
    assert [(item["path"], item["body"]) for item in seen] == [("/v1/unregister", {"version": 1})]
    claims = jwt.decode(seen[0]["authorization"].removeprefix("Bearer "),
                        options={"verify_signature": False})
    assert claims["iss"] == "ins_0000000007"
    assert "revoked AutoIDM cloud registration ins_0000000007" in capsys.readouterr().out


@pytest.mark.parametrize("answer", [(401, {"error": "unauthorized"}), (503, {})])
def test_a_refused_or_failed_revoke_only_warns(backend, capsys, answer):
    url, seen, answers = backend
    answers.append(answer)
    deploy_common.revoke_registration(cloud_secret(url))
    assert len(seen) == 1
    assert "AutoIDM did not revoke the cloud registration ins_0000000007" in " ".join(
        capsys.readouterr().out.split())


def test_an_unreachable_autoidm_or_a_broken_key_only_warns(capsys):
    deploy_common.revoke_registration(cloud_secret("http://127.0.0.1:9"))
    broken = json.loads(cloud_secret("http://127.0.0.1:9"))
    deploy_common.revoke_registration(json.dumps({**broken, autoidm.KEY_ENV: "not a key"}))
    assert capsys.readouterr().out.count("AutoIDM did not revoke the cloud registration") == 2


@pytest.mark.parametrize("current", [None, "not json", json.dumps({"A": "1"}),
                                     json.dumps({**REGISTRATION, autoidm.KEY_ENV: ""})])
def test_a_secret_with_no_cloud_registration_sends_nothing(backend, current, capsys):
    _url, seen, _answers = backend
    deploy_common.revoke_registration(current)
    assert seen == [] and capsys.readouterr().out == ""


class FakeSecrets:
    def __init__(self, value):
        self.value, self.calls = value, []

    def get_secret_value(self, SecretId):
        self.calls.append(("get", SecretId))
        return {"SecretString": self.value}


def test_an_aws_destroy_reads_the_secret_only_with_managed_services(backend, monkeypatch):
    from pdt import deploy_aws_batch
    url, seen, _answers = backend
    deleted = []
    monkeypatch.setattr(deploy_aws_batch, "delete_secret",
                        lambda secrets, name: deleted.append(name))
    secrets = FakeSecrets(cloud_secret(url))
    monkeypatch.setattr(deploy_aws_batch, "reads_registration", lambda: False)
    deploy_aws_batch.delete_app_secret(secrets, "pdt-demo")
    assert secrets.calls == [] and seen == [] and deleted == ["pdt-demo"]
    monkeypatch.setattr(deploy_aws_batch, "reads_registration", lambda: True)
    deploy_aws_batch.delete_app_secret(secrets, "pdt-demo")
    assert secrets.calls == [("get", "pdt-demo")] and deleted == ["pdt-demo", "pdt-demo"]
    assert [item["path"] for item in seen] == ["/v1/unregister"]


def test_an_aws_destroy_deletes_the_secret_when_autoidm_cannot_be_reached(monkeypatch, capsys):
    from pdt import deploy_aws_batch
    deleted = []
    monkeypatch.setattr(deploy_aws_batch, "delete_secret",
                        lambda secrets, name: deleted.append(name))
    monkeypatch.setattr(deploy_aws_batch, "reads_registration", lambda: True)
    deploy_aws_batch.delete_app_secret(FakeSecrets(cloud_secret("http://127.0.0.1:9")),
                                       "pdt-demo")
    assert deleted == ["pdt-demo"]
    assert "AutoIDM did not revoke" in capsys.readouterr().out


def test_a_google_cloud_destroy_revokes_from_the_latest_secret_version(backend, monkeypatch):
    from pdt import deploy_google_cloud
    url, seen, _answers = backend
    data = base64.b64encode(cloud_secret(url).encode()).decode()
    asked = []

    def api_json(address):
        asked.append(address)
        return {"payload": {"data": data}}

    monkeypatch.setattr(deploy_google_cloud, "api_json", api_json)
    deploy_google_cloud.revoke_app_registration("example-jobs", "pdt-demo")
    assert asked == [f"{deploy_google_cloud.SECRET_API}/projects/example-jobs/secrets/pdt-demo/"
                     "versions/latest:access"]
    assert [item["path"] for item in seen] == ["/v1/unregister"]


def test_a_google_cloud_secret_pdt_cannot_read_only_warns(monkeypatch, capsys):
    from pdt import deploy_google_cloud

    def api_json(address):
        raise deploy_google_cloud.ApiReadError(address)

    monkeypatch.setattr(deploy_google_cloud, "api_json", api_json)
    deploy_google_cloud.revoke_app_registration("example-jobs", "pdt-demo")
    assert "AutoIDM keeps its cloud registration" in capsys.readouterr().out


def test_an_azure_destroy_revokes_from_the_key_vault_secret(backend, monkeypatch, capsys):
    from pdt import deploy_azure
    url, seen, _answers = backend
    monkeypatch.setattr(deploy_azure, "access_token", lambda resource=None: "token")
    monkeypatch.setattr(deploy_azure, "arm_get",
                        lambda token, address, command, missing=(404,): {
                            "value": cloud_secret(url)})
    deploy_azure.revoke_app_registration({"vault": "pdt-vault"}, "pdt-demo")
    assert [item["path"] for item in seen] == ["/v1/unregister"]

    def refused(token, address, command, missing=(404,)):
        raise deploy_azure.ReadError(command, "ERROR: Forbidden")

    monkeypatch.setattr(deploy_azure, "arm_get", refused)
    deploy_azure.revoke_app_registration({"vault": "pdt-vault"}, "pdt-demo")
    assert "AutoIDM keeps its cloud registration" in " ".join(capsys.readouterr().out.split())
