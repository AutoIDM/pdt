import io
import json

import pytest
from cryptography.hazmat.primitives.serialization import load_pem_private_key
from test_autoidm import backend, folder, registered  # noqa: F401

from pdt import deploy_common, managed_services
from pdt.utils import autoidm

CUSTOMER = "cus_7a1c09e4b2"
REGISTRATION = {autoidm.ID_ENV: "ins_0000000007", autoidm.KEY_ENV: "-----PEM-----",
                autoidm.URL_ENV: "https://api.example"}
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
    assert (made["A"], made[autoidm.ID_ENV], made[autoidm.URL_ENV]) == (
        "1", "ins_0000000008", url)
    key = load_pem_private_key(made[autoidm.KEY_ENV].encode(), password=None)
    assert autoidm.public_key(key) == body["public_key"]
    assert "made AutoIDM cloud registration ins_0000000008" in capsys.readouterr().out
    assert sorted(path.suffix for path in autoidm.folder().iterdir()) == [".json", ".key"]


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
