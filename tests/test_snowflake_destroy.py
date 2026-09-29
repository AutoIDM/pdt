import pytest

from pdt import deploy_snowflake
from pdt.deploy_snowflake import Session

MARK = "managed-by=pdt"
TASK = "PDT.PDT_HELLO_WORLD.RUN"
STEPS = [
    f"ALTER TASK IF EXISTS {TASK} SUSPEND",
    f"DROP TASK IF EXISTS {TASK}",
    "DROP SERVICE IF EXISTS PDT.PDT_HELLO_WORLD.RUN_20260929_010101",
    "DROP SCHEMA IF EXISTS PDT.PDT_HELLO_WORLD",
]


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setattr(deploy_snowflake, "ensure_session", lambda app: Session(
        conn=object(), account="myorg-myacct", region="AWS_US_EAST_1", role="SYSADMIN",
        user="jon", has_warehouse=True))
    return {"name": "hello-world", "dir": tmp_path, "storage": False,
            "platform": {"provider": "snowflake"}}


def record_sql(monkeypatch, answers):
    calls = []

    def fake_run(session, sql, file_stream=None):
        calls.append(sql)
        for prefix, rows in answers:
            if sql.startswith(prefix):
                return rows
        return []

    monkeypatch.setattr(deploy_snowflake, "run", fake_run)
    return calls


def deployed(app_schema_comment=MARK, other_schemas=()):
    schemas = [{"name": "INFORMATION_SCHEMA", "comment": ""}, {"name": "PUBLIC", "comment": ""},
               {"name": "PDT_HELLO_WORLD", "comment": app_schema_comment},
               *[{"name": name, "comment": MARK} for name in other_schemas]]
    return [
        ("SHOW DATABASES LIKE 'PDT'", [{"name": "PDT", "comment": MARK}]),
        ("SHOW DATABASES LIKE 'PDT_DATA'",
         [{"name": "PDT_DATA", "comment": f"{MARK} pdt-lifecycle=retain"}]),
        ("SHOW SCHEMAS LIKE 'PDT_HELLO_WORLD'", [{"name": "PDT_HELLO_WORLD", "comment": app_schema_comment}]),
        ("SHOW SCHEMAS IN DATABASE PDT", schemas),
        ("SHOW JOB SERVICES", [{"name": "RUN_20260929_010101"}]),
        ("SHOW WAREHOUSES", [{"name": "PDT", "comment": MARK}]),
        ("SHOW COMPUTE POOLS", [{"name": "PDT", "comment": MARK}]),
        ("SHOW INTEGRATIONS", [{"name": "PDT", "comment": MARK}]),
    ]


def changes(calls):
    return [call for call in calls if not call.startswith("SHOW ")]


def test_destroying_one_of_two_apps_removes_only_its_schema(app, monkeypatch, capsys):
    calls = record_sql(monkeypatch, deployed(other_schemas=["PDT_OTHER"]))
    assert deploy_snowflake.destroy(app, assume_yes=True) == 0
    assert changes(calls) == STEPS
    out = capsys.readouterr().out
    assert f"delete task {TASK}" in out
    assert "delete 1 job service(s) of PDT.PDT_HELLO_WORLD" in out
    assert "PDT apps still deployed: PDT_OTHER." in out
    assert "compute pool PDT" in out.split("Still present:")[1]


def test_destroying_the_last_app_removes_the_shared_objects_in_order(app, monkeypatch, capsys):
    calls = record_sql(monkeypatch, deployed())
    assert deploy_snowflake.destroy(app, assume_yes=True) == 0
    assert changes(calls) == [
        *STEPS,
        "DROP INTEGRATION IF EXISTS PDT",
        "ALTER COMPUTE POOL PDT STOP ALL",
        "DROP COMPUTE POOL IF EXISTS PDT",
        "DROP WAREHOUSE IF EXISTS PDT",
        "DROP DATABASE IF EXISTS PDT",
    ]
    out = capsys.readouterr().out
    assert "delete compute pool PDT (no other apps use it)" in out
    assert "database PDT_DATA" in out.split("Still present:")[1]


def test_destroying_an_app_that_is_not_deployed_drops_nothing(app, monkeypatch, capsys):
    calls = record_sql(monkeypatch, [])
    assert deploy_snowflake.destroy(app, assume_yes=True) == 0
    assert changes(calls) == []
    assert "Nothing to remove for hello-world" in capsys.readouterr().out


def test_a_schema_without_the_marker_is_kept_and_keeps_the_database(app, monkeypatch, capsys):
    calls = record_sql(monkeypatch, deployed(app_schema_comment="mine"))
    assert deploy_snowflake.destroy(app, assume_yes=True) == 0
    assert changes(calls) == []
    out = capsys.readouterr().out
    assert "schema PDT.PDT_HELLO_WORLD is not managed by PDT; keeping it" in out
    assert "schema PDT.PDT_HELLO_WORLD" in out.split("Still present:")[-1]
