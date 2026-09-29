"""One way to open a Snowflake session, on the user's computer and inside a job.

Inside a Snowpark Container Services job Snowflake writes an OAuth token to
/snowflake/session/token and sets SNOWFLAKE_HOST and SNOWFLAKE_ACCOUNT, and
the session runs as the role that owns the job (the role that deployed it).
Anywhere else the session comes from the environment: SNOWFLAKE_ACCOUNT,
SNOWFLAKE_USER, and one of SNOWFLAKE_PRIVATE_KEY_FILE (key pair),
SNOWFLAKE_PASSWORD (a password or a programmatic access token), or nothing,
which opens the browser login. SNOWFLAKE_ROLE picks the role when set.

`pdt.utils.storage` and `pdt.utils.env_secret` connect through here, so a
job needs no credentials of its own.
"""

from __future__ import annotations

import os
from pathlib import Path

TOKEN_FILE = Path("/snowflake/session/token")
ENV = {
    "account": "SNOWFLAKE_ACCOUNT",
    "user": "SNOWFLAKE_USER",
    "password": "SNOWFLAKE_PASSWORD",
    "private_key_file": "SNOWFLAKE_PRIVATE_KEY_FILE",
    "private_key_file_pwd": "SNOWFLAKE_PRIVATE_KEY_FILE_PWD",
    "role": "SNOWFLAKE_ROLE",
}


def in_job() -> bool:
    return TOKEN_FILE.is_file()


def connect_kwargs(**overrides) -> dict:
    """The connect() arguments the environment and `overrides` decide."""
    if in_job():
        return {"host": os.environ["SNOWFLAKE_HOST"],
                "account": os.environ["SNOWFLAKE_ACCOUNT"],
                "token": TOKEN_FILE.read_text().strip(),
                "authenticator": "oauth"}
    kwargs = {}
    for key, name in ENV.items():
        value = os.environ.get(name, "").strip()
        if value != "":
            kwargs[key] = value
    kwargs.update({key: value for key, value in overrides.items() if value is not None})
    if kwargs.get("private_key_file"):
        kwargs["authenticator"] = "SNOWFLAKE_JWT"
        kwargs.pop("password", None)
    elif not kwargs.get("password"):
        kwargs["authenticator"] = "externalbrowser"
        # Keeps the browser login's id token, so the next command needs no browser.
        kwargs.setdefault("client_store_temporary_credential", True)
    return kwargs


def connect(**overrides):
    import snowflake.connector
    return snowflake.connector.connect(**connect_kwargs(**overrides))


def execute(conn, sql: str, file_stream=None) -> list[dict]:
    """Run one statement and return its rows as dicts, column names in lower case."""
    from snowflake.connector import DictCursor
    with conn.cursor(DictCursor) as cursor:
        cursor.execute(sql, file_stream=file_stream)
        return [{str(key).lower(): value for key, value in row.items()}
                for row in cursor.fetchall()]


def literal(text: str) -> str:
    """`text` as a Snowflake single-quoted string literal."""
    escaped = str(text).replace("\\", "\\\\").replace("'", "''")
    return f"'{escaped}'"
