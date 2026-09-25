import subprocess

import pytest

from conftest import add_app
from pdt import config, deploy_google_cloud
from pdt.utils import email_auth


class Result:
    def __init__(self, returncode, stdout=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = ""


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


def choose_project_answering(project, monkeypatch, answer):
    add_app(project, "my-report", "schedule: daily\n")
    monkeypatch.setattr(email_auth, "can_prompt", lambda interactive: True)
    monkeypatch.setattr("builtins.input", lambda prompt="": answer)
    saved = []

    def record_save(app, key, value):
        saved.append((key, value))
        return project / "pdt.yml"

    monkeypatch.setattr(config, "save_platform_key", record_save)
    record_gcloud(monkeypatch, [(["projects", "list"],
                                 Result(0, "first-project\tFirst\nsecond-project\tSecond\n"))])
    return saved


def test_a_typed_project_id_from_the_list_is_saved(project, monkeypatch):
    saved = choose_project_answering(project, monkeypatch, "second-project")
    assert deploy_google_cloud.choose_project({"name": "my-report"}, "") == "second-project"
    assert saved == [("project", "second-project")]


def test_a_number_still_picks_a_project_by_position(project, monkeypatch):
    saved = choose_project_answering(project, monkeypatch, "1")
    assert deploy_google_cloud.choose_project({"name": "my-report"}, "") == "first-project"
    assert saved == [("project", "first-project")]


LISTED = Result(0, "first-project\tFirst\nsecond-project\tSecond\n")
ONE_ACCOUNT = '[{"name": "billingAccounts/AAAA-1111", "displayName": "Main"}]'
TWO_ACCOUNTS = ('[{"name": "billingAccounts/AAAA-1111", "displayName": "Main"},'
                ' {"name": "billingAccounts/BBBB-2222", "displayName": "Side"}]')


def create_answering(project, monkeypatch, answers, accounts, create=True):
    add_app(project, "my-report", "schedule: daily\n")
    monkeypatch.setattr(email_auth, "can_prompt", lambda interactive: True)
    replies = iter(answers)
    monkeypatch.setattr("builtins.input", lambda prompt="": next(replies))
    monkeypatch.setattr(deploy_google_cloud.console, "confirm", lambda question="": create)
    saved = []

    def record_save(app, key, value):
        saved.append((key, value))
        return project / "pdt.yml"

    monkeypatch.setattr(config, "save_platform_key", record_save)
    calls = record_gcloud(monkeypatch, [
        (["projects", "list"], LISTED),
        (["projects", "create"], Result(0)),
        (["billing", "accounts", "list"], Result(0, accounts)),
        (["billing", "projects", "link"], Result(0)),
    ])
    return calls, saved


def gcloud_calls(calls, *prefix):
    return [call[1:] for call in calls if call[1:1 + len(prefix)] == list(prefix)]


def test_a_typed_new_id_creates_the_project_and_links_the_only_billing_account(project, monkeypatch):
    calls, saved = create_answering(project, monkeypatch, ["brand-new-project"], ONE_ACCOUNT)
    assert deploy_google_cloud.choose_project({"name": "my-report"}, "") == "brand-new-project"
    assert gcloud_calls(calls, "projects", "create") == [
        ["projects", "create", "brand-new-project", "--name", "brand-new-project"]]
    assert gcloud_calls(calls, "billing", "projects", "link") == [
        ["billing", "projects", "link", "brand-new-project", "--billing-account", "AAAA-1111"]]
    assert saved == [("project", "brand-new-project")]


def test_a_typed_new_id_with_two_billing_accounts_links_the_chosen_one(project, monkeypatch):
    calls, saved = create_answering(project, monkeypatch, ["brand-new-project", "2"], TWO_ACCOUNTS)
    assert deploy_google_cloud.choose_project({"name": "my-report"}, "") == "brand-new-project"
    assert gcloud_calls(calls, "billing", "projects", "link") == [
        ["billing", "projects", "link", "brand-new-project", "--billing-account", "BBBB-2222"]]
    assert saved == [("project", "brand-new-project")]


def test_a_typed_new_id_with_no_open_billing_account_names_the_billing_console(
        project, monkeypatch, capsys):
    calls, saved = create_answering(project, monkeypatch, ["brand-new-project"], "[]")
    with pytest.raises(SystemExit):
        deploy_google_cloud.choose_project({"name": "my-report"}, "")
    assert gcloud_calls(calls, "billing", "projects", "link") == []
    assert saved == []
    assert "https://console.cloud.google.com/billing" in capsys.readouterr().out


def test_a_badly_formed_id_fails_before_any_other_gcloud_call(project, monkeypatch, capsys):
    calls, saved = create_answering(project, monkeypatch, ["Bad_Project"], ONE_ACCOUNT)
    with pytest.raises(SystemExit):
        deploy_google_cloud.choose_project({"name": "my-report"}, "")
    assert [call[1:3] for call in calls] == [["projects", "list"]]
    assert saved == []
    assert "Bad_Project is not a valid Google Cloud project id" in capsys.readouterr().out


def test_declining_the_create_question_creates_nothing(project, monkeypatch, capsys):
    calls, saved = create_answering(project, monkeypatch, ["brand-new-project"], ONE_ACCOUNT,
                                    create=False)
    with pytest.raises(SystemExit):
        deploy_google_cloud.choose_project({"name": "my-report"}, "")
    assert gcloud_calls(calls, "projects", "create") == []
    assert saved == []
    assert "no Google Cloud project selected" in capsys.readouterr().out
