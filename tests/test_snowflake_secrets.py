from contextlib import nullcontext

import pytest

from pdt import deploy_common, deploy_snowflake
from pdt.deploy_snowflake import Session
from pdt.utils import env_secret, snowflake_connection

NAMES = deploy_snowflake.names("hello-world")


@pytest.fixture
def app(tmp_path, monkeypatch):
    session = Session(conn=object(), account="myorg-myacct", region="AWS_US_EAST_1",
                      role="SYSADMIN", user="jon", has_warehouse=True)
    monkeypatch.setattr(deploy_snowflake, "ensure_session", lambda app: session)
    monkeypatch.setattr(deploy_snowflake, "schema_exists", lambda session, schema: True)
    monkeypatch.setattr(deploy_snowflake, "find", lambda *args: {"name": "ENV", "comment": "managed-by=pdt"})
    monkeypatch.setattr(deploy_snowflake, "read_secret", lambda session, names: '{"TOKEN": "old"}')
    monkeypatch.setattr(deploy_common, "gather_secrets", lambda app: {"TOKEN": "new"})
    return {"name": "hello-world", "dir": tmp_path, "platform": {"provider": "snowflake"}}


def record_sql(monkeypatch):
    calls = []
    monkeypatch.setattr(deploy_snowflake, "run",
                        lambda session, sql, file_stream=None: calls.append(sql) or [])
    return calls


def test_secrets_diff_shows_the_changed_value_and_writes_nothing(app, monkeypatch, capsys):
    calls = record_sql(monkeypatch)
    assert deploy_snowflake.secrets(app, "diff", True) == 0
    out = capsys.readouterr().out
    assert "updated" in out
    assert "TOKEN" in out
    assert "pdt secrets hello-world save" in out
    assert calls == []


def test_secrets_save_writes_the_env_values_into_the_secret(app, monkeypatch):
    calls = record_sql(monkeypatch)
    assert deploy_snowflake.secrets(app, "save", True) == 0
    assert calls == ["ALTER SECRET PDT.PDT_HELLO_WORLD.ENV SET SECRET_STRING = '{\"TOKEN\": \"new\"}'"]


def test_secrets_of_a_secret_pdt_cannot_read_point_at_a_deploy(app, monkeypatch, capsys):
    monkeypatch.setattr(deploy_snowflake, "read_secret", lambda session, names: None)
    with pytest.raises(SystemExit):
        deploy_snowflake.secrets(app, "diff", True)
    assert "Run `pdt deploy hello-world` first." in capsys.readouterr().out


def test_a_new_secret_is_created_with_the_marker(monkeypatch):
    calls = record_sql(monkeypatch)
    deploy_snowflake.write_secret(None, NAMES, '{"A": "1"}', exists=False)
    assert calls == ["CREATE SECRET PDT.PDT_HELLO_WORLD.ENV TYPE = GENERIC_STRING "
                     "SECRET_STRING = '{\"A\": \"1\"}' COMMENT = 'managed-by=pdt'"]


def test_a_job_reads_its_secret_from_the_mounted_variable(monkeypatch):
    backend = env_secret.SnowflakeSecret("snow://PDT.PDT_X.ENV")
    monkeypatch.setenv("PDT_ENV_JSON", '{"TOKEN": "mounted"}')
    assert backend.read() == {"TOKEN": "mounted"}
    monkeypatch.delenv("PDT_ENV_JSON")
    assert backend.read() == {}


def test_a_job_writes_its_secret_with_one_alter_statement(monkeypatch):
    conn = object()
    calls = []
    monkeypatch.setattr(snowflake_connection, "connect", lambda **overrides: nullcontext(conn))
    monkeypatch.setattr(snowflake_connection, "execute",
                        lambda conn, sql, file_stream=None: calls.append((conn, sql)) or [])
    env_secret.SnowflakeSecret("snow://PDT.PDT_X.ENV").write({"B": "it's", "A": "1"})
    assert calls == [(conn, "ALTER SECRET PDT.PDT_X.ENV SET SECRET_STRING = "
                            "'{\"A\": \"1\", \"B\": \"it''s\"}'")]
