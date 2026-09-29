import yaml

from pdt import deploy_snowflake

NAMES = deploy_snowflake.names("hello-world")


def test_the_task_submits_the_job_without_waiting_for_it():
    body = deploy_snowflake.task_body(NAMES)
    assert body.startswith("BEGIN")
    assert "ASYNC = TRUE" in body
    assert "EXTERNAL_ACCESS_INTEGRATIONS = (PDT)" in body
    assert "FROM @PDT.PDT_HELLO_WORLD.SPEC" in body
    assert "SPECIFICATION_FILE = ''spec.yaml''" in body


def test_the_job_spec_runs_one_container_with_the_secret_and_the_store():
    spec = yaml.safe_load(deploy_snowflake.job_spec(NAMES, True, True))["spec"]
    [container] = spec["containers"]
    assert container["name"] == "main"
    assert container["image"] == "/pdt/pdt_hello_world/images/hello-world:latest"
    assert container["env"] == {"PDT_ENV_SECRET_RESOURCE": "snow://PDT.PDT_HELLO_WORLD.ENV",
                                "PDT_STORAGE_URL": "snow://PDT_DATA.PUBLIC.PDT_DATA/hello-world/"}
    assert container["secrets"] == [{"snowflakeSecret": {"objectName": "PDT.PDT_HELLO_WORLD.ENV"},
                                     "envVarName": "PDT_ENV_JSON",
                                     "secretKeyRef": "secret_string"}]
    assert spec["logExporters"]["eventTableConfig"]["logLevel"] == "INFO"


def test_the_job_spec_of_an_app_with_no_secret_and_no_store_has_no_env_and_no_secrets():
    spec = yaml.safe_load(deploy_snowflake.job_spec(NAMES, False, False))["spec"]
    [container] = spec["containers"]
    assert "env" not in container
    assert "secrets" not in container


def test_an_object_is_managed_when_its_comment_carries_the_marker():
    assert deploy_snowflake.managed({"comment": "managed-by=pdt"})
    assert deploy_snowflake.managed({"comment": "managed-by=pdt pdt-app=hello-world"})
    assert not deploy_snowflake.managed({"comment": "mine"})
    assert not deploy_snowflake.managed({"comment": None})
    assert not deploy_snowflake.managed(None)


def test_a_literal_doubles_quotes_and_backslashes():
    assert deploy_snowflake.literal("it's a \\ test") == "'it''s a \\\\ test'"


def test_the_grant_statements_cover_every_privilege_and_the_events_role():
    statements = deploy_snowflake.grant_statements("R")
    assert statements == [
        *[f"GRANT {privilege} ON ACCOUNT TO ROLE R;"
          for privilege in deploy_snowflake.ACCOUNT_PRIVILEGES],
        "GRANT APPLICATION ROLE SNOWFLAKE.EVENTS_VIEWER TO ROLE R;",
    ]
