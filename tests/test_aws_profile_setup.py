import hashlib
import json
import os
import stat
from pathlib import Path

import boto3
import pytest
from botocore.exceptions import ClientError
from botocore.utils import JSONFileCache, SSOTokenLoader

from pdt import config, deploy_aws

URL = "https://d-1234567890.awsapps.com/start"
PORTAL = Path(__file__).resolve().parent / "data" / "aws_portal_start.html"


class OIDC:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)

    def register_client(self, **kwargs):
        return {"clientId": "client", "clientSecret": "secret", "clientSecretExpiresAt": 1791000000}

    def start_device_authorization(self, **kwargs):
        return {"deviceCode": "device", "userCode": "ABCD-EFGH", "interval": 1, "expiresIn": 600,
                "verificationUriComplete": "https://device.sso.example/?user_code=ABCD-EFGH"}

    def create_token(self, **kwargs):
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, str):
            raise ClientError({"Error": {"Code": outcome, "Message": outcome}}, "CreateToken")
        return outcome


class Elsewhere:
    def register_client(self, **kwargs):
        raise ClientError({"Error": {"Code": "InvalidRequestException",
                                     "Message": "Invalid request"}}, "RegisterClient")


class Pages:
    def __init__(self, pages):
        self.pages = pages

    def paginate(self, **kwargs):
        return self.pages(**kwargs)


class SSO:
    def __init__(self, accounts, roles):
        self.accounts = accounts
        self.roles = roles

    def get_paginator(self, name):
        if name == "list_accounts":
            return Pages(lambda **kwargs: [{"accountList": self.accounts}])
        return Pages(lambda accountId, **kwargs: [{"roleList": self.roles[accountId]}])


TOKEN = {"accessToken": "access", "expiresIn": 3600, "refreshToken": "refresh"}


@pytest.fixture
def aws(project, tmp_path, monkeypatch):
    (project / "pdt.yml").write_text("platform:\n  provider: aws\n")
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("AWS_CONFIG_FILE", str(home / ".aws" / "config"))
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(home / ".aws" / "credentials"))
    for name in ("AWS_PROFILE", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(deploy_aws, "can_ask", lambda: True)
    monkeypatch.setattr(deploy_aws.webbrowser, "open", lambda url: True)
    monkeypatch.setattr(deploy_aws, "portal_html", no_portal)
    return home


def no_portal(start_url):
    raise OSError("no network in tests")


def answers(monkeypatch, *replies):
    queue = list(replies)
    monkeypatch.setattr("builtins.input", lambda prompt="": queue.pop(0))
    return queue


def clients(monkeypatch, oidc, sso=None, accepts="us-east-1"):
    """Fake AWS clients; IAM Identity Center accepts the start URL only in `accepts`."""
    made = []

    def client(service, region_name, **kwargs):
        made.append((service, region_name))
        if service == "sso":
            return sso
        return oidc if region_name == accepts else Elsewhere()

    monkeypatch.setattr(deploy_aws.boto3, "client", client)
    return made


def app(project, region="eu-west-1"):
    return {"name": "hello", "dir": project / "hello",
            "platform": {"provider": "aws", "region": region}}


def test_sso_setup_writes_a_profile_botocore_reads(aws, project, monkeypatch, capsys):
    answers(monkeypatch, "1", URL)
    clients(monkeypatch, OIDC([TOKEN]),
            SSO([{"accountId": "123456789012", "accountName": "Prod"}],
                {"123456789012": [{"roleName": "Deployer"}]}), accepts="eu-west-1")
    profile = deploy_aws.choose_profile(app(project), boto3.Session())
    assert profile == "Deployer-123456789012"
    settings = boto3.Session(profile_name=profile)._session.get_scoped_config()
    assert settings["sso_account_id"] == "123456789012"
    assert settings["sso_role_name"] == "Deployer"
    assert settings["region"] == "eu-west-1"
    assert deploy_aws.sso_target(profile) == ("d-1234567890", URL, "eu-west-1",
                                              "sso-session d-1234567890")
    assert "Your IAM Identity Center is in: eu-west-1" in capsys.readouterr().out
    cache = aws / ".aws" / "sso" / "cache" / (hashlib.sha1(b"d-1234567890").hexdigest() + ".json")
    token = json.loads(cache.read_text())
    assert token["accessToken"] == "access"
    assert token["refreshToken"] == "refresh"
    assert token["expiresAt"].endswith("Z")
    assert {"startUrl", "region", "clientId", "clientSecret", "registrationExpiresAt"} <= set(token)
    assert config.load_yaml(project / "pdt.yml")["platform"]["profile"] == profile


def test_the_token_cache_is_what_botocore_loads(aws, monkeypatch):
    clients(monkeypatch, OIDC([TOKEN]))
    deploy_aws.sso_sign_in("corp", URL, "us-east-1", "sso-session corp")
    loader = SSOTokenLoader(cache=JSONFileCache(str(aws / ".aws" / "sso" / "cache")))
    assert loader(URL, session_name="corp")["accessToken"] == "access"


def test_an_existing_sso_session_section_is_not_written_twice(aws, project, monkeypatch):
    config_file = aws / ".aws" / "config"
    config_file.parent.mkdir()
    config_file.write_text(f"# mine\n[sso-session d-1234567890]\nsso_start_url = {URL}\n"
                           "sso_region = eu-west-1\n")
    answers(monkeypatch, "1", URL)
    clients(monkeypatch, OIDC([TOKEN]),
            SSO([{"accountId": "123456789012", "accountName": "Prod"}],
                {"123456789012": [{"roleName": "Deployer"}]}))
    deploy_aws.create_profile(app(project))
    text = config_file.read_text()
    assert text.startswith("# mine\n")
    assert text.count("[sso-session d-1234567890]") == 1


def test_a_pasted_portal_url_is_cleaned(aws, project, monkeypatch):
    answers(monkeypatch, "1", f'  "{URL}/#/"  \n')
    clients(monkeypatch, OIDC([TOKEN]),
            SSO([{"accountId": "123456789012", "accountName": "Prod"}],
                {"123456789012": [{"roleName": "Deployer"}]}))
    profile = deploy_aws.create_profile(app(project))
    assert deploy_aws.sso_target(profile)[:3] == ("d-1234567890", URL, "us-east-1")


def test_several_accounts_and_roles_are_numbered_choices(aws, project, monkeypatch):
    answers(monkeypatch, "1", URL, "2", "Reader")
    clients(monkeypatch, OIDC([TOKEN]),
            SSO([{"accountId": "111111111111", "accountName": "Dev"},
                 {"accountId": "222222222222", "accountName": "Prod"}],
                {"222222222222": [{"roleName": "Admin"}, {"roleName": "Reader"}]}),
            accepts="us-west-2")
    profile = deploy_aws.create_profile(app(project, region=""))
    assert profile == "Reader-222222222222"
    assert deploy_aws.sso_target(profile)[:3] == ("d-1234567890", URL, "us-west-2")
    assert "region" not in boto3.Session(profile_name=profile)._session.get_scoped_config()


def test_sign_in_waits_while_pending_and_slows_down_when_asked(aws, monkeypatch, capsys):
    oidc = OIDC(["AuthorizationPendingException", "SlowDownException", TOKEN])
    clients(monkeypatch, oidc)
    sleeps = []
    started = deploy_aws.sso_start(URL, "us-east-1")
    assert deploy_aws.sso_finish(started, None, URL, "us-east-1", sleep=sleeps.append) == "access"
    assert sleeps == [1, 6]
    out = capsys.readouterr().out
    assert "ABCD-EFGH" in out
    assert (aws / ".aws" / "sso" / "cache" / (hashlib.sha1(URL.encode()).hexdigest() + ".json")).is_file()


def test_a_refused_sign_in_fails_plainly(aws, monkeypatch, capsys):
    clients(monkeypatch, OIDC(["AccessDeniedException"]))
    with pytest.raises(SystemExit):
        deploy_aws.sso_sign_in(None, URL, "us-east-1", "profile work")
    assert "AWS sign-in failed" in capsys.readouterr().out


def test_access_key_setup_writes_a_private_default_profile(aws, project, monkeypatch):
    answers(monkeypatch, "2", "AKIAEXAMPLE")
    monkeypatch.setattr(deploy_aws.getpass, "getpass", lambda prompt: "hidden-secret")
    profile = deploy_aws.choose_profile(app(project), boto3.Session())
    assert profile == "default"
    credentials = aws / ".aws" / "credentials"
    assert "[default]\naws_access_key_id = AKIAEXAMPLE\n" in credentials.read_text()
    assert boto3.Session(profile_name="default").get_credentials().secret_key == "hidden-secret"
    if os.name != "nt":
        assert stat.S_IMODE(credentials.stat().st_mode) == 0o600
    assert config.load_yaml(project / "pdt.yml")["platform"]["profile"] == "default"


def test_no_terminal_names_pdt_yml_and_asks_nothing(aws, project, monkeypatch, capsys):
    monkeypatch.setattr(deploy_aws, "can_ask", lambda: False)
    answers(monkeypatch)
    with pytest.raises(SystemExit):
        deploy_aws.choose_profile(app(project), boto3.Session())
    out = capsys.readouterr().out
    assert "profile: <name>" in out
    assert "pdt.yml" in out


class Identity:
    def get_caller_identity(self):
        return {"Arn": "arn:aws:sts::123456789012:assumed-role/Deployer/me",
                "Account": "123456789012"}


def no_cli():
    raise AssertionError("the AWS CLI ran")


@pytest.fixture
def signed_in(monkeypatch):
    real = boto3.Session

    class Session(real):
        def client(self, service, **kwargs):
            return Identity() if service == "sts" else super().client(service, **kwargs)

    monkeypatch.setattr(deploy_aws.boto3, "Session", Session)
    monkeypatch.setattr(deploy_aws, "aws_cli", no_cli)
    monkeypatch.setattr(deploy_aws.subprocess, "run", lambda *args, **kwargs: no_cli())


def test_relogin_signs_in_an_sso_profile_with_boto3(aws, project, monkeypatch, signed_in, capsys):
    config_file = aws / ".aws" / "config"
    config_file.parent.mkdir()
    config_file.write_text(f"[sso-session corp]\nsso_start_url = {URL}\nsso_region = us-east-1\n\n"
                           "[profile work]\nsso_session = corp\nsso_account_id = 123456789012\n"
                           "sso_role_name = Deployer\n")
    oidc = OIDC([TOKEN])
    made = clients(monkeypatch, oidc)
    target = app(project)
    target["platform"]["profile"] = "work"
    assert deploy_aws.relogin(target) == 0
    assert made == [("sso-oidc", "us-east-1")]
    assert "Signed in as" in capsys.readouterr().out


def test_relogin_after_creating_a_profile_signs_in_once(aws, project, monkeypatch, signed_in):
    oidc = OIDC([TOKEN])
    answers(monkeypatch, "1", URL)
    clients(monkeypatch, oidc,
            SSO([{"accountId": "123456789012", "accountName": "Prod"}],
                {"123456789012": [{"roleName": "Deployer"}]}))
    assert deploy_aws.relogin(app(project)) == 0
    assert oidc.outcomes == []


def test_the_portal_page_names_the_region(aws, monkeypatch):
    monkeypatch.setattr(deploy_aws, "portal_html", lambda start_url: PORTAL.read_text())
    made = clients(monkeypatch, OIDC([TOKEN]))
    region, started = deploy_aws.find_sso_region(URL, "eu-west-1")
    assert region == "us-east-1"
    assert made == [("sso-oidc", "us-east-1")]


@pytest.mark.parametrize("page", [None, "<!doctype html><html><head></head></html>"])
def test_the_probe_finds_the_one_region_that_accepts(aws, monkeypatch, page):
    if page is not None:
        monkeypatch.setattr(deploy_aws, "portal_html", lambda start_url: page)
    made = clients(monkeypatch, OIDC([TOKEN]), accepts="ap-southeast-2")
    region, started = deploy_aws.find_sso_region(URL, "eu-west-1")
    assert region == "ap-southeast-2"
    assert {("sso-oidc", "eu-west-1"), ("sso-oidc", "us-east-1")} <= set(made)
    assert started[2]["userCode"] == "ABCD-EFGH"


def test_no_region_accepts_the_url_without_a_terminal(aws, monkeypatch, capsys):
    monkeypatch.setattr(deploy_aws, "can_ask", lambda: False)
    clients(monkeypatch, OIDC([TOKEN]), accepts="nowhere")
    with pytest.raises(SystemExit):
        deploy_aws.find_sso_region(URL, "")
    assert "did not accept the access portal URL" in capsys.readouterr().out


def test_no_region_accepts_the_url_so_pdt_asks(aws, monkeypatch):
    answers(monkeypatch, "xx-test-1")
    clients(monkeypatch, OIDC([TOKEN]), accepts="xx-test-1")
    region, started = deploy_aws.find_sso_region(URL, "")
    assert region == "xx-test-1"


def test_relogin_with_the_wrong_region_names_the_config_section(aws, project, monkeypatch,
                                                                signed_in, capsys):
    config_file = aws / ".aws" / "config"
    config_file.parent.mkdir()
    config_file.write_text(f"[sso-session corp]\nsso_start_url = {URL}\nsso_region = eu-west-1\n\n"
                           "[profile work]\nsso_session = corp\nsso_account_id = 123456789012\n"
                           "sso_role_name = Deployer\n")
    clients(monkeypatch, OIDC([TOKEN]), accepts="us-east-1")
    target = app(project)
    target["platform"]["profile"] = "work"
    with pytest.raises(SystemExit):
        deploy_aws.relogin(target)
    out = " ".join(capsys.readouterr().out.split())
    assert "do not match" in out
    assert "[sso-session corp]" in out
    assert "config" in out
