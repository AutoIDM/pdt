import pytest

from conftest import add_app
from pdt.config import aws_account_problem, validate_app


@pytest.mark.parametrize("account", ["123456789012", " 123456789012 "])
def test_a_real_account_id_passes(account):
    assert aws_account_problem(account) == ""


@pytest.mark.parametrize("account", ["my-group", "12345", "1234567890123", "12345678901a"])
def test_a_wrong_account_id_is_reported(account):
    assert aws_account_problem(account) != ""


def test_an_empty_account_is_allowed_because_deploy_fills_it_in():
    assert aws_account_problem("") == ""


def test_validate_names_the_key_the_user_must_fix(project):
    (project / "pdt.yml").write_text("platform:\n  provider: aws\n  account: my-group\n")
    add_app(project, "my-report", "schedule: daily\n")
    problems = validate_app("my-report")
    assert any("platform.account" in p and "12 digits" in p for p in problems)


def test_a_fresh_aws_project_validates_clean_before_the_first_deploy(project):
    (project / "pdt.yml").write_text("platform:\n  provider: aws\n  region: us-east-1\n")
    add_app(project, "my-report", "schedule: daily\n")
    assert not any("account" in p for p in validate_app("my-report"))


def test_missing_provider_says_where_to_set_it(project):
    (project / "pdt.yml").write_text("platform: {}\n")
    add_app(project, "my-report", "schedule: daily\n")
    problems = validate_app("my-report")
    assert any("pdt.yml" in p for p in problems)
