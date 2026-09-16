import pytest

from conftest import add_app
from pdt.config import azure_environment_problem, validate_app


@pytest.mark.parametrize("value", ["", "my-group/my-env", " rg1/env-1 "])
def test_a_group_and_a_name_pass(value):
    assert azure_environment_problem(value) == ""


@pytest.mark.parametrize("value", ["my-env", "/my-env", "my-group/", "a/b/c", "my group/env"])
def test_a_value_without_one_slash_is_reported(value):
    assert azure_environment_problem(value) != ""


def test_validate_names_the_key_the_user_must_fix(project):
    (project / "pdt.yml").write_text("platform:\n  provider: azure\n  environment: my-env\n")
    add_app(project, "my-report", "schedule: daily\n")
    problems = validate_app("my-report")
    assert any("platform.environment" in p and "<resource-group>/<name>" in p for p in problems)


