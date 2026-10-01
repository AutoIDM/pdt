from pathlib import Path

import pytest

from conftest import add_app
from pdt import config, deploy_snowflake
from pdt.deploy_snowflake import Session
from pdt.utils import snowflake_connection

FACTS = {"org": "myorg", "account": "myacct", "region": "PUBLIC.AWS_US_EAST_1",
         "role": "SYSADMIN", "user": "jon"}


@pytest.fixture(autouse=True)
def clean_env(tmp_path, monkeypatch):
    for name in (*snowflake_connection.ENV.values(), "SNOWFLAKE_HOST"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(snowflake_connection, "TOKEN_FILE", tmp_path / "no-token")


def session():
    return Session(conn=object(), account="myorg-myacct", region="AWS_US_EAST_1", role="SYSADMIN",
                   user="jon", has_warehouse=True)


def test_without_credentials_the_login_is_the_browser_with_a_cached_token():
    assert snowflake_connection.connect_kwargs(account="a", user="u") == {
        "account": "a", "user": "u", "authenticator": "externalbrowser",
        "client_store_temporary_credential": True}


def test_a_key_pair_file_signs_in_with_a_jwt_and_drops_the_password(monkeypatch):
    monkeypatch.setenv("SNOWFLAKE_PRIVATE_KEY_FILE", "/keys/pdt.p8")
    monkeypatch.setenv("SNOWFLAKE_PASSWORD", "secret")
    kwargs = snowflake_connection.connect_kwargs(account="a", user="u")
    assert kwargs["authenticator"] == "SNOWFLAKE_JWT"
    assert kwargs["private_key_file"] == "/keys/pdt.p8"
    assert "password" not in kwargs


def test_a_password_signs_in_without_an_authenticator(monkeypatch):
    monkeypatch.setenv("SNOWFLAKE_PASSWORD", "secret")
    assert snowflake_connection.connect_kwargs(account="a", user="u") == {
        "account": "a", "user": "u", "password": "secret"}


def test_an_argument_beats_the_environment(monkeypatch):
    monkeypatch.setenv("SNOWFLAKE_ACCOUNT", "from-env")
    monkeypatch.setenv("SNOWFLAKE_USER", "env-user")
    kwargs = snowflake_connection.connect_kwargs(account="from-arg")
    assert (kwargs["account"], kwargs["user"]) == ("from-arg", "env-user")


def test_inside_a_job_the_session_token_is_the_only_credential(tmp_path, monkeypatch):
    token = tmp_path / "token"
    token.write_text("tok\n")
    monkeypatch.setattr(snowflake_connection, "TOKEN_FILE", token)
    monkeypatch.setenv("SNOWFLAKE_HOST", "myorg-myacct.snowflakecomputing.com")
    monkeypatch.setenv("SNOWFLAKE_ACCOUNT", "myorg-myacct")
    assert snowflake_connection.connect_kwargs() == {
        "host": "myorg-myacct.snowflakecomputing.com", "account": "myorg-myacct",
        "token": "tok", "authenticator": "oauth"}


def snow_app():
    return {"name": "hello-world", "platform": {"account": "myorg-myacct", "user": "jon"}}


def test_the_snow_command_for_a_browser_login_names_the_account_user_and_role():
    command = deploy_snowflake.snow_command(session(), snow_app())
    assert Path(command[0]).stem == "snow"
    assert command[1] == "--temporary-connection"
    assert command[command.index("--account") + 1] == "myorg-myacct"
    assert command[command.index("--user") + 1] == "jon"
    assert command[command.index("--role") + 1] == "SYSADMIN"
    assert command[command.index("--authenticator") + 1] == "externalbrowser"
    assert "--private-key-file" not in command


def test_the_snow_command_for_a_key_pair_passes_the_key_file(monkeypatch):
    monkeypatch.setenv("SNOWFLAKE_PRIVATE_KEY_FILE", "/keys/pdt.p8")
    command = deploy_snowflake.snow_command(session(), snow_app())
    assert command[command.index("--private-key-file") + 1] == "/keys/pdt.p8"
    assert "--authenticator" not in command


def test_the_snow_command_never_carries_the_password(monkeypatch):
    monkeypatch.setenv("SNOWFLAKE_PASSWORD", "secret")
    command = deploy_snowflake.snow_command(session(), snow_app())
    assert not any("secret" in part for part in command)
    assert "--authenticator" not in command


def test_a_run_with_no_account_and_no_one_to_ask_names_the_setting_and_the_variable(monkeypatch, capsys):
    monkeypatch.setattr(deploy_snowflake, "can_ask", lambda: False)
    with pytest.raises(SystemExit):
        deploy_snowflake.ensure_session({"name": "hello-world", "platform": {"provider": "snowflake"}})
    out = capsys.readouterr().out
    assert "platform.account" in out
    assert "SNOWFLAKE_ACCOUNT" in out


def test_a_browser_login_with_no_one_to_ask_names_the_other_ways_to_sign_in(monkeypatch, capsys):
    monkeypatch.setattr(deploy_snowflake, "can_ask", lambda: False)
    app = {"name": "hello-world", "platform": {"account": "myorg-myacct", "user": "jon"}}
    with pytest.raises(SystemExit):
        deploy_snowflake.ensure_session(app)
    out = capsys.readouterr().out
    assert "SNOWFLAKE_PRIVATE_KEY_FILE" in out
    assert "SNOWFLAKE_PASSWORD" in out


def sign_in(monkeypatch, facts, warehouses=()):
    calls = []
    monkeypatch.setattr(deploy_snowflake, "can_ask", lambda: True)
    monkeypatch.setattr(snowflake_connection, "connect", lambda **overrides: object())
    monkeypatch.setattr(snowflake_connection, "execute", lambda conn, sql, file_stream=None: [facts])

    def fake_run(session, sql, file_stream=None):
        calls.append(sql)
        return list(warehouses) if sql.startswith("SHOW WAREHOUSES") else []

    monkeypatch.setattr(deploy_snowflake, "run", fake_run)
    return calls


def test_an_account_that_differs_from_the_sign_in_fails_naming_both(monkeypatch, capsys):
    sign_in(monkeypatch, FACTS)
    app = {"name": "hello-world", "platform": {"account": "other-acct", "user": "jon"}}
    with pytest.raises(SystemExit):
        deploy_snowflake.ensure_session(app)
    out = capsys.readouterr().out
    assert "other-acct" in out
    assert "myorg-myacct" in out


def test_the_first_sign_in_writes_the_region_back(project, monkeypatch):
    (project / "pdt.yml").write_text(
        "platform:\n  provider: snowflake\n  account: myorg-myacct\n  user: jon\n")
    add_app(project, "hello-world", "schedule: daily\n")
    sign_in(monkeypatch, FACTS)
    session = deploy_snowflake.ensure_session(config.merged_app("hello-world"))
    assert session.region == "AWS_US_EAST_1"
    assert session.account == "myorg-myacct"
    assert 'region: "AWS_US_EAST_1"' in (project / "pdt.yml").read_text()


def test_a_managed_warehouse_is_switched_on_at_sign_in(project, monkeypatch):
    (project / "pdt.yml").write_text(
        "platform:\n  provider: snowflake\n  account: myorg-myacct\n  user: jon\n")
    add_app(project, "hello-world", "schedule: daily\n")
    calls = sign_in(monkeypatch, FACTS, [{"name": "PDT", "comment": "managed-by=pdt"}])
    session = deploy_snowflake.ensure_session(config.merged_app("hello-world"))
    assert session.has_warehouse
    assert "USE WAREHOUSE PDT" in calls
