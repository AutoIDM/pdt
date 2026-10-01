from conftest import add_app
from pdt import config, deploy_snowflake


def test_an_app_name_becomes_an_unquoted_identifier():
    assert deploy_snowflake.object_name("hello-world") == "PDT_HELLO_WORLD"


def test_every_object_of_an_app_lives_in_its_own_schema():
    assert deploy_snowflake.names("hello-world") == {
        "schema": "PDT_HELLO_WORLD",
        "schema_fqn": "PDT.PDT_HELLO_WORLD",
        "repository": "PDT.PDT_HELLO_WORLD.IMAGES",
        "spec_stage": "PDT.PDT_HELLO_WORLD.SPEC",
        "secret": "PDT.PDT_HELLO_WORLD.ENV",
        "function": "PDT.PDT_HELLO_WORLD.READ_ENV",
        "task": "PDT.PDT_HELLO_WORLD.RUN",
        "image": "/pdt/pdt_hello_world/images/hello-world:latest",
        "store_url": "snow://PDT_DATA.PUBLIC.PDT_DATA/hello-world/",
        "secret_resource": "snow://PDT.PDT_HELLO_WORLD.ENV",
    }


def test_an_app_name_must_fit_an_unquoted_identifier():
    for name in ("hello-world", "report_2"):
        assert config.snowflake_app_name_problem(name) == ""
    for name in ("1st", "a.b", "héllo"):
        assert config.snowflake_app_name_problem(name) != ""


def test_an_account_may_be_empty_or_either_identifier_form():
    for account in ("", "myorg-myacct", "xy12345.us-east-1"):
        assert config.snowflake_account_problem(account) == ""
    assert config.snowflake_account_problem("my org") != ""


def test_a_bad_account_is_reported_under_platform_account(project):
    (project / "pdt.yml").write_text(
        "platform:\n  provider: snowflake\n  account: bad account\n")
    add_app(project, "hello-world", "schedule: daily\n")
    problems = config.validate_app("hello-world")
    assert len(problems) == 1
    assert problems[0].startswith("hello-world: platform.account:")


def test_an_app_folder_that_cannot_name_objects_is_reported(project):
    (project / "pdt.yml").write_text("platform:\n  provider: snowflake\n")
    add_app(project, "1st-app", "schedule: daily\n")
    problems = config.validate_app("1st-app")
    assert len(problems) == 1
    assert "cannot name Snowflake objects" in problems[0]
