import pytest

from pdt import deploy_snowflake
from pdt.deploy_common import CostEstimate
from pdt.deploy_snowflake import Session

MARK = "managed-by=pdt"
PAYLOAD = '{"TOKEN": "x"}'
SCOPE = "IN SCHEMA PDT.PDT_HELLO_WORLD"


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setattr(deploy_snowflake, "ensure_session", lambda app: Session(
        conn=object(), account="myorg-myacct", region="AWS_US_EAST_1", role="SYSADMIN",
        user="jon", has_warehouse=True))
    monkeypatch.setattr(deploy_snowflake, "gather_secrets", lambda app: {"TOKEN": "x"})
    monkeypatch.setattr(deploy_snowflake, "cost_estimate", lambda *args: CostEstimate(
        [("compute pool", 1.0)], "test prices"))
    return {"name": "hello-world", "dir": tmp_path, "schedule": "0 0 * * *",
            "timezone": "Etc/UTC", "storage": True, "platform": {"provider": "snowflake"}}


def record_sql(monkeypatch, answers):
    calls = []

    def fake_run(session, sql, file_stream=None):
        calls.append(sql)
        for prefix, rows in answers:
            if sql.startswith(prefix):
                return rows
        return []

    monkeypatch.setattr(deploy_snowflake, "run", fake_run)
    monkeypatch.setattr(deploy_snowflake, "build_image",
                        lambda session, app, names: calls.append("build image"))
    return calls


def exists(name):
    return [{"name": name, "comment": MARK}]


def test_a_first_deploy_prints_every_action_in_the_plan(app, monkeypatch, capsys):
    record_sql(monkeypatch, [])
    assert deploy_snowflake.deploy(app, assume_yes=True) == 0
    plan = capsys.readouterr().out.split("Plan:")[1].split("Estimated monthly cost")[0]
    for line in (
        "create database PDT",
        "create warehouse PDT (XSMALL, suspends after 60 s)",
        "create compute pool PDT (CPU_X64_XS, 1 node, suspends after 60 s)",
        "create external access integration PDT (any host, port 443)",
        "create stage PDT_DATA.PUBLIC.PDT_DATA (kept after destroy)",
        "create schema PDT.PDT_HELLO_WORLD",
        "create image repository PDT.PDT_HELLO_WORLD.IMAGES",
        "build image /pdt/pdt_hello_world/images/hello-world:latest in Snowflake",
        "create secret PDT.PDT_HELLO_WORLD.ENV (1 env vars as one json blob)",
        "create function PDT.PDT_HELLO_WORLD.READ_ENV",
        "upload the job specification to stage PDT.PDT_HELLO_WORLD.SPEC",
        'create task PDT.PDT_HELLO_WORLD.RUN: "0 0 * * *" (Etc/UTC)',
    ):
        assert f"  {line}" in plan


def test_a_first_deploy_creates_the_objects_in_dependency_order(app, monkeypatch):
    calls = record_sql(monkeypatch, [])
    deploy_snowflake.deploy(app, assume_yes=True)
    statements = [
        "CREATE DATABASE PDT DATA_RETENTION_TIME_IN_DAYS = 0",
        "CREATE TAG PDT.PUBLIC.MANAGED_BY",
        "CREATE NETWORK RULE PDT.PUBLIC.PDT_EGRESS MODE = EGRESS TYPE = HOST_PORT "
        "VALUE_LIST = ('0.0.0.0:443')",
        "CREATE WAREHOUSE PDT WAREHOUSE_SIZE = XSMALL",
        "CREATE COMPUTE POOL PDT MIN_NODES = 1 MAX_NODES = 1 INSTANCE_FAMILY = CPU_X64_XS "
        "AUTO_SUSPEND_SECS = 60",
        "CREATE EXTERNAL ACCESS INTEGRATION PDT",
        "CREATE DATABASE PDT_DATA DATA_RETENTION_TIME_IN_DAYS = 0",
        "CREATE STAGE PDT_DATA.PUBLIC.PDT_DATA",
        "CREATE SCHEMA PDT.PDT_HELLO_WORLD",
        "CREATE IMAGE REPOSITORY PDT.PDT_HELLO_WORLD.IMAGES",
        "build image",
        "CREATE SECRET PDT.PDT_HELLO_WORLD.ENV TYPE = GENERIC_STRING",
        "CREATE OR REPLACE FUNCTION PDT.PDT_HELLO_WORLD.READ_ENV",
        "PUT 'file://spec.yaml' @PDT.PDT_HELLO_WORLD.SPEC",
        "CREATE OR REPLACE TASK PDT.PDT_HELLO_WORLD.RUN SCHEDULE = 'USING CRON 0 0 * * * Etc/UTC'",
        "ALTER TASK PDT.PDT_HELLO_WORLD.RUN RESUME",
    ]
    positions = []
    for statement in statements:
        found = [i for i, call in enumerate(calls) if call.startswith(statement)]
        assert found, f"no statement starts with {statement}"
        positions.append(found[0])
    assert positions == sorted(positions)
    [function] = [call for call in calls if call.startswith("CREATE OR REPLACE FUNCTION")]
    assert "SECRETS = ('env' = PDT.PDT_HELLO_WORLD.ENV)" in function


def test_a_redeploy_of_an_unchanged_app_creates_no_shared_object_and_keeps_the_secret(app, monkeypatch, capsys):
    calls = record_sql(monkeypatch, [
        ("SHOW DATABASES LIKE 'PDT'", exists("PDT")),
        ("SHOW DATABASES LIKE 'PDT_DATA'", exists("PDT_DATA")),
        ("SHOW WAREHOUSES", exists("PDT")),
        ("SHOW COMPUTE POOLS", exists("PDT")),
        ("SHOW INTEGRATIONS", exists("PDT")),
        ("SHOW SCHEMAS LIKE 'PDT_HELLO_WORLD'", exists("PDT_HELLO_WORLD")),
        (f"SHOW IMAGE REPOSITORIES LIKE 'IMAGES' {SCOPE}", exists("IMAGES")),
        (f"SHOW SECRETS LIKE 'ENV' {SCOPE}", exists("ENV")),
        (f"SHOW TASKS LIKE 'RUN' {SCOPE}", exists("RUN")),
        ("SHOW USER FUNCTIONS", [{"name": "READ_ENV"}]),
    ])
    monkeypatch.setattr(deploy_snowflake, "read_secret", lambda session, names: PAYLOAD)
    monkeypatch.setattr(deploy_snowflake, "deployer_store", lambda session, name: type(
        "Store", (), {"usage": lambda self: (0, 0)})())
    assert deploy_snowflake.deploy(app, assume_yes=True) == 0
    plan = capsys.readouterr().out.split("Plan:")[1]
    assert "use existing database PDT" in plan
    assert "use existing compute pool PDT" in plan
    assert "unchanged secret PDT.PDT_HELLO_WORLD.ENV" in plan
    assert 'update task PDT.PDT_HELLO_WORLD.RUN: "0 0 * * *" (Etc/UTC)' in plan
    changes = [call for call in calls if not call.startswith("SHOW ")]
    assert not any(call.startswith(("CREATE DATABASE", "CREATE COMPUTE POOL", "CREATE SECRET",
                                    "ALTER SECRET")) for call in changes)
    assert any(call.startswith("CREATE OR REPLACE TASK") for call in changes)


def test_an_existing_database_without_the_marker_stops_the_deploy(app, monkeypatch, capsys):
    calls = record_sql(monkeypatch, [
        ("SHOW DATABASES LIKE 'PDT'", [{"name": "PDT", "comment": "someone else's"}]),
    ])
    with pytest.raises(SystemExit) as caught:
        deploy_snowflake.deploy(app, assume_yes=True)
    assert caught.value.code == 1
    assert "database PDT exists but is not managed by PDT" in capsys.readouterr().out
    assert not any(not call.startswith("SHOW ") for call in calls)
