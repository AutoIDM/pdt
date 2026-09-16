import subprocess

import pytest

from conftest import add_app
from pdt import config, deploy_google_cloud
from pdt.utils import email_auth


class Result:
    def __init__(self, returncode, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def record_gcloud(monkeypatch, answers):
    calls = []

    def fake_run(command, *args, **kwargs):
        calls.append(list(command))
        for prefix, result in answers:
            if list(command)[1:1 + len(prefix)] == list(prefix):
                return result() if callable(result) else result
        return Result(1)

    monkeypatch.setattr(subprocess, "run", fake_run)
    return calls


def refuse_prompt(monkeypatch):
    def fake_input(prompt=""):
        raise AssertionError(f"pdt asked a question: {prompt}")

    monkeypatch.setattr("builtins.input", fake_input)


def test_a_microsoft_smtp_app_with_a_cached_token_needs_cache_updates():
    values = {"PDT_SMTP_HOST": "smtp.office365.com",
              "PDT_SMTP_OAUTH_CACHE_B64": "abc"}
    assert deploy_google_cloud.needs_oauth_cache_updates(values) is True


def test_a_graph_app_needs_cache_updates_without_any_smtp_host():
    assert deploy_google_cloud.needs_oauth_cache_updates(
        {"PDT_GRAPH_MAIL_CACHE_B64": "abc"}) is True


def test_an_app_with_neither_token_cache_needs_no_cache_updates():
    assert deploy_google_cloud.needs_oauth_cache_updates(
        {"PDT_SMTP_HOST": "smtp.office365.com", "PDT_TOKEN": "t-1"}) is False
    assert deploy_google_cloud.needs_oauth_cache_updates({}) is False


def test_an_existing_access_token_is_enough_and_nothing_else_runs(monkeypatch):
    refuse_prompt(monkeypatch)
    calls = record_gcloud(monkeypatch, [(["auth", "print-access-token"], Result(0, "ya29.a0\n"))])
    deploy_google_cloud.ensure_credentials()
    assert calls == [[deploy_google_cloud.GCLOUD, "auth", "print-access-token"]]


def test_a_service_account_key_file_signs_in_without_a_question(monkeypatch, tmp_path):
    refuse_prompt(monkeypatch)
    key_file = tmp_path / "key.json"
    key_file.write_text('{"type": "service_account"}')
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", str(key_file))
    tokens = iter([Result(1), Result(0, "ya29.a0\n")])
    calls = record_gcloud(monkeypatch, [
        (["auth", "print-access-token"], lambda: next(tokens)),
        (["--quiet", "auth", "login"], Result(0)),
    ])
    deploy_google_cloud.ensure_credentials()
    assert [deploy_google_cloud.GCLOUD, "--quiet", "auth", "login",
            "--cred-file", str(key_file)] in calls


def test_no_credential_and_no_key_file_names_both_ways_to_fix_it(monkeypatch, capsys):
    refuse_prompt(monkeypatch)
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)
    record_gcloud(monkeypatch, [])
    with pytest.raises(SystemExit):
        deploy_google_cloud.ensure_credentials()
    message = capsys.readouterr().out
    assert "GOOGLE_APPLICATION_CREDENTIALS" in message
    assert "auth login" in message


def test_a_person_at_a_terminal_is_still_offered_the_browser_login(monkeypatch):
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)
    monkeypatch.setattr(email_auth, "can_prompt", lambda interactive: True)
    monkeypatch.setattr("builtins.input", lambda prompt="": "y")
    calls = record_gcloud(monkeypatch, [(["auth", "login"], Result(0))])
    deploy_google_cloud.ensure_credentials()
    assert [deploy_google_cloud.GCLOUD, "auth", "login"] in calls


def test_an_unattended_run_with_no_project_names_the_key_to_set(project, monkeypatch, capsys):
    refuse_prompt(monkeypatch)
    add_app(project, "my-report", "schedule: daily\n")

    def refuse_save(app, key, value):
        raise AssertionError("pdt rewrote a config file")

    monkeypatch.setattr(config, "save_platform_key", refuse_save)
    record_gcloud(monkeypatch, [])
    with pytest.raises(SystemExit):
        deploy_google_cloud.choose_project({"name": "my-report"}, "")
    message = capsys.readouterr().out
    assert "platform.project" in message
    assert "PDT_GOOGLE_CLOUD_PROJECT" in message


def test_a_missing_cloud_run_job_is_absent(monkeypatch):
    record_gcloud(monkeypatch, [
        (["run", "jobs", "describe"], Result(1, stderr="Cannot find job [pdt-report].")),
    ])
    assert deploy_google_cloud.read_json_or_none(
        "run", "jobs", "describe", "pdt-report") is None


def test_a_cloud_run_permission_error_still_fails(monkeypatch):
    record_gcloud(monkeypatch, [
        (["run", "jobs", "describe"], Result(1, stderr="PERMISSION_DENIED")),
    ])
    with pytest.raises(SystemExit):
        deploy_google_cloud.read_json_or_none("run", "jobs", "describe", "pdt-report")


def test_service_account_lookup_uses_the_account_list(monkeypatch):
    monkeypatch.setattr(deploy_google_cloud, "list_json", lambda *args: [
        {"email": "other@example.test"},
        {"email": "pdt-runner@example.test", "displayName": "pdt job runner"},
    ])

    account = deploy_google_cloud.service_account_or_none(
        "example-project", "pdt-runner@example.test")

    assert account["displayName"] == "pdt job runner"


def test_missing_service_account_is_absent(monkeypatch):
    monkeypatch.setattr(deploy_google_cloud, "list_json", lambda *args: [])

    assert deploy_google_cloud.service_account_or_none(
        "example-project", "pdt-runner@example.test") is None
