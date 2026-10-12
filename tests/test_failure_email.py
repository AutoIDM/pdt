"""The failure email's settings on every command path: secrets, validate, and deploy."""

import os

import pytest

from conftest import add_app
from pdt import cli, config, deploy, deploy_common
from pdt.utils import send_email

APP_YAML = ("schedule: daily\nconfig:\n  email_from: pdt@example.com\n"
            "notify:\n  email:\n    to: ops@example.com\n")
NO_OPTION = ("notify.email.to asks for a failure email, but no email option is set; "
             "add PDT_SMTP_HOST, PDT_SES_REGION, PDT_RESEND_API_KEY, or "
             "PDT_GRAPH_MAIL_USER to the .env file")


@pytest.fixture(autouse=True)
def restore_environ():
    # python-dotenv writes straight into os.environ, past monkeypatch.
    saved = dict(os.environ)
    for name in send_email.EMAIL_VARS:
        os.environ.pop(name, None)
    os.environ.pop("PDT_ENV_SECRET_RESOURCE", None)
    yield
    os.environ.clear()
    os.environ.update(saved)


def run_cli(monkeypatch, *argv):
    monkeypatch.setattr("sys.argv", ["pdt", *argv])
    return cli.main()


def test_an_app_sends_a_failure_email_only_when_on_failure_and_a_recipient_ask_for_it(project):
    add_app(project, "on", APP_YAML)
    add_app(project, "off", APP_YAML + "on_failure: []\n")
    add_app(project, "nobody", "schedule: daily\n")
    add_app(project, "blank", "schedule: daily\nnotify:\n  email:\n    to: ' '\n")
    assert config.sends_failure_email(config.merged_app("on"))
    assert not config.sends_failure_email(config.merged_app("off"))
    assert not config.sends_failure_email(config.merged_app("nobody"))
    assert not config.sends_failure_email(config.merged_app("blank"))


def test_the_job_gets_the_email_settings_its_env_spec_does_not_list(project):
    add_app(project, "my-report", APP_YAML)
    os.environ.update({"PDT_SMTP_HOST": "smtp.example.com", "PDT_SMTP_USER": "pdt",
                       "PDT_SMTP_PASSWORD": "p", "UNRELATED": "x"})
    assert deploy_common.gather_secrets(config.merged_app("my-report")) == {
        "PDT_SMTP_HOST": "smtp.example.com", "PDT_SMTP_USER": "pdt", "PDT_SMTP_PASSWORD": "p"}


def test_an_app_that_sends_no_email_gets_no_email_settings(project):
    add_app(project, "my-report", "schedule: daily\n")
    os.environ.update({"PDT_SMTP_HOST": "smtp.example.com", "PDT_SMTP_USER": "pdt"})
    assert deploy_common.gather_secrets(config.merged_app("my-report")) == {}


def test_validate_names_the_missing_email_option_of_a_failure_email(project, monkeypatch,
                                                                     capsys):
    add_app(project, "my-report", APP_YAML)
    assert run_cli(monkeypatch, "validate") == 1
    assert f"my-report: {NO_OPTION}" in " ".join(capsys.readouterr().out.split())


def test_validate_names_a_missing_sender_of_a_failure_email(project, monkeypatch, capsys):
    add_app(project, "my-report", "schedule: daily\nnotify:\n  email:\n    to: ops@example.com\n")
    os.environ["PDT_RESEND_API_KEY"] = "re_1"
    assert run_cli(monkeypatch, "validate") == 1
    assert ("my-report: email_from is required when sending email; set it in config.yml"
            in " ".join(capsys.readouterr().out.split()))


def test_validate_passes_a_complete_failure_email(project, monkeypatch):
    add_app(project, "my-report", APP_YAML)
    os.environ["PDT_RESEND_API_KEY"] = "re_1"
    assert run_cli(monkeypatch, "validate") == 0


def test_deploy_stops_before_dispatch_without_an_email_option(project, monkeypatch, capsys):
    monkeypatch.setattr(deploy, "dispatch", lambda *a, **k: pytest.fail("dispatched"))
    add_app(project, "my-report", APP_YAML)
    assert deploy.deploy("my-report", assume_yes=True) == 1
    assert f"my-report: {NO_OPTION}" in " ".join(capsys.readouterr().out.split())


def test_deploy_completes_the_email_authorization_of_a_failure_email(project, monkeypatch):
    prepared = []
    monkeypatch.setattr(deploy, "prepare_email_auth", prepared.append)
    monkeypatch.setattr(deploy, "dispatch", lambda *a, **k: 1)
    monkeypatch.setattr(deploy.regions, "choose_region", lambda *a: "")
    add_app(project, "my-report", APP_YAML)
    os.environ["PDT_RESEND_API_KEY"] = "re_1"
    deploy.deploy("my-report", assume_yes=True)
    assert len(prepared) == 1
