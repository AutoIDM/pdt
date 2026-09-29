import sys
import types

import pytest

from pdt import deploy_snowflake
from pdt.deploy_snowflake import Session
from pdt.utils import snowflake_connection


@pytest.fixture
def refuse(monkeypatch):
    class Error(Exception):
        pass

    errors = types.ModuleType("snowflake.connector.errors")
    errors.Error = Error
    for name in ("snowflake", "snowflake.connector"):
        monkeypatch.setitem(sys.modules, name, types.ModuleType(name))
    monkeypatch.setitem(sys.modules, "snowflake.connector.errors", errors)

    def raise_error(message):
        def execute(conn, sql, file_stream=None):
            raise Error(message)

        monkeypatch.setattr(snowflake_connection, "execute", execute)

    return raise_error


def run_refused(sql="CREATE DATABASE PDT"):
    session = Session(conn=object(), account="myorg-myacct", region="AWS_US_EAST_1",
                      role="SYSADMIN", user="jon", has_warehouse=True)
    with pytest.raises(SystemExit) as caught:
        deploy_snowflake.run(session, sql)
    assert caught.value.code == 1


def test_a_missing_privilege_prints_the_grants_for_the_current_role(refuse, capsys):
    refuse("SQL access control error: Insufficient privileges to operate on account")
    run_refused()
    out = capsys.readouterr().out
    assert "Current Snowflake role: SYSADMIN" in out
    assert "GRANT CREATE COMPUTE POOL ON ACCOUNT TO ROLE SYSADMIN;" in out
    assert "GRANT APPLICATION ROLE SNOWFLAKE.EVENTS_VIEWER TO ROLE SYSADMIN;" in out


def test_a_trial_account_is_told_it_cannot_run_containers(refuse, capsys):
    refuse("Feature not available for trial accounts")
    run_refused()
    assert "trial account" in capsys.readouterr().out


def test_any_other_error_prints_its_message_and_the_statement(refuse, capsys):
    refuse("Object 'PDT' already exists with another type")
    run_refused("CREATE DATABASE PDT")
    out = capsys.readouterr().out
    assert "Object 'PDT' already exists with another type" in out
    assert "Snowflake refused `CREATE DATABASE PDT`" in out
