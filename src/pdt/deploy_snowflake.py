#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "snowflake-connector-python",
#     "snowflake-cli",
#     "pyyaml",
#     "rich",
#     "python-dotenv",
#     "backoff",
#     "fsspec",
#     "duckdb",
# ]
# ///
"""Deploy an app to Snowflake as a scheduled Snowpark Container Services job.

Every object lives in the database PDT. Per app, in the schema PDT.PDT_<APP>:
  image repository  IMAGES     the app's image, built by Snowflake itself
  stage             SPEC       the job specification
  secret            ENV        every env var as one json blob (GENERIC_STRING)
  function          READ_ENV   reads the secret back, because SQL cannot
  task              RUN        the schedule; each run is a job service RUN_<time>
Shared across apps:
  compute pool      PDT        one CPU_X64_XS node, suspended after 60 s idle
  integration       PDT        egress to any host on port 443 (network rule
                               PDT.PUBLIC.PDT_EGRESS); a container has none by
                               default
  warehouse         PDT        XSMALL, suspended after 60 s; pdt uses it to
                               read logs, the secret, and the rate sheet
  tag               PDT.PUBLIC.MANAGED_BY = 'pdt' on every object that takes
                               a tag; COMMENT 'managed-by=pdt' on every object
  database          PDT_DATA   the data store, stage PDT_DATA.PUBLIC.PDT_DATA,
                               one folder per app (kept after destroy)

An unquoted Snowflake name cannot hold a hyphen, so per-app objects are named
PDT_<APP> with underscores instead of pdt-<app>. Both databases keep
DATA_RETENTION_TIME_IN_DAYS = 0, so a dropped object leaves nothing in Time
Travel.

The task submits the job with ASYNC = TRUE and ends. A serverless task is
billed for as long as its statement runs, so waiting for the job would cost
0.9 credits an hour on top of the compute pool. `pdt runs` reads the job
services themselves, so nothing is lost by not waiting.

Deploy reconciles: it creates what is missing and updates what changed, so it
is safe to re-run after a failure. Secrets and the image build context are
prepared by pdt/deploy_common.py.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from pdt import config, console, runs_cli, storage_cli
from pdt.deploy import confirm
from pdt.deploy_common import (
    CostEstimate, fail, gather_secrets, image_action, run_secrets, stage_build_context,
    store_cost_label, store_kept_line, warn_if_locked, write_dockerfile)
from pdt.utils import email_auth, snowflake_connection
from pdt.utils.snowflake_connection import literal
from pdt.utils.storage import Store

DATABASE = "PDT"
DATA_DATABASE = "PDT_DATA"
DATA_STAGE = f"{DATA_DATABASE}.PUBLIC.PDT_DATA"
POOL = "PDT"
WAREHOUSE = "PDT"
INTEGRATION = "PDT"
NETWORK_RULE = f"{DATABASE}.PUBLIC.PDT_EGRESS"
TAG = f"{DATABASE}.PUBLIC.MANAGED_BY"
INSTANCE_FAMILY = "CPU_X64_XS"
IDLE_SECONDS = 60
MARK = "managed-by=pdt"
STORE_MARK = f"{MARK} pdt-lifecycle=retain"
SPEC_FILE = "spec.yaml"
RUN_PREFIX = "RUN_"
JOB_STATUSES = {"DONE": "succeeded", "FAILED": "failed", "CANCELLED": "failed",
                "INTERNAL_ERROR": "failed"}
ACCOUNT_PRIVILEGES = ("CREATE DATABASE", "CREATE WAREHOUSE", "CREATE COMPUTE POOL",
                      "CREATE INTEGRATION", "EXECUTE TASK", "EXECUTE MANAGED TASK")
PERMISSION_MARKERS = ("insufficient privileges", "not authorized", "access control error")

# Snowflake Service Consumption Table, effective January 21, 2026:
# Table 1(e) CPU_X64_XS 0.06 credits per hour; Table 5 Serverless Tasks
# multiplier 0.9 on one credit per XSMALL compute-hour; Table 2 on demand
# credit price (Standard edition) and Table 3(a) standard storage per TB a
# month, by region as CURRENT_REGION() names it.
CONSUMPTION_TABLE = "Snowflake Service Consumption Table of January 21, 2026"
POOL_CREDITS_PER_HOUR = 0.06
TASK_CREDITS_PER_HOUR = 0.9
TASK_SECONDS = 10.0
ASSUMED_RUN_MINUTES = 5.0
RECENT_RUNS = 3
LIST_PRICES = {
    "AWS_US_EAST_1": (2.00, 23.00),
    "AWS_US_EAST_2": (2.00, 23.00),
    "AWS_US_WEST_2": (2.00, 23.00),
    "AWS_CA_CENTRAL_1": (2.25, 25.00),
    "AWS_EU_WEST_1": (2.60, 23.00),
    "AWS_EU_CENTRAL_1": (2.60, 24.50),
    "AWS_EU_WEST_2": (2.70, 24.00),
    "AWS_EU_NORTH_1": (2.40, 23.00),
    "AWS_AP_SOUTHEAST_1": (2.50, 25.00),
    "AWS_AP_SOUTHEAST_2": (2.75, 25.00),
    "AWS_AP_NORTHEAST_1": (2.85, 25.00),
    "AWS_AP_SOUTH_1": (2.00, 23.00),
    "AZURE_EASTUS2": (2.00, 23.00),
    "AZURE_WESTUS2": (2.00, 23.00),
    "AZURE_CENTRALUS": (2.00, 23.00),
    "AZURE_WESTEUROPE": (2.60, 23.00),
    "GCP_US_CENTRAL1": (2.00, 23.00),
    "GCP_US_EAST4": (2.00, 23.00),
    "GCP_EUROPE_WEST4": (2.60, 23.00),
}
DEFAULT_LIST_PRICE = "AWS_US_EAST_1"


@dataclass
class Session:
    conn: object
    account: str
    region: str
    role: str
    user: str
    has_warehouse: bool


def object_name(app_name: str) -> str:
    return "PDT_" + app_name.upper().replace("-", "_")


def names(app_name: str) -> dict[str, str]:
    schema = object_name(app_name)
    return {
        "schema": schema,
        "schema_fqn": f"{DATABASE}.{schema}",
        "repository": f"{DATABASE}.{schema}.IMAGES",
        "spec_stage": f"{DATABASE}.{schema}.SPEC",
        "secret": f"{DATABASE}.{schema}.ENV",
        "function": f"{DATABASE}.{schema}.READ_ENV",
        "task": f"{DATABASE}.{schema}.RUN",
        "image": f"/{DATABASE.lower()}/{schema.lower()}/images/{app_name}:latest",
        "store_url": f"snow://{DATA_STAGE}/{app_name}/",
        "secret_resource": f"snow://{DATABASE}.{schema}.ENV",
    }


def managed(row: dict | None) -> bool:
    return row is not None and MARK in str(row.get("comment") or "")


def require_managed(row: dict | None, label: str) -> None:
    if row is not None and not managed(row):
        fail(f"{label} exists but is not managed by PDT")


def can_ask() -> bool:
    return email_auth.can_prompt(None)


def ask(prompt: str) -> str:
    try:
        return input(prompt).strip()
    except EOFError:
        return ""


def grant_statements(role: str) -> list[str]:
    lines = [f"GRANT {privilege} ON ACCOUNT TO ROLE {role};" for privilege in ACCOUNT_PRIVILEGES]
    lines.append(f"GRANT APPLICATION ROLE SNOWFLAKE.EVENTS_VIEWER TO ROLE {role};")
    return lines


def print_permission_help(role: str, detail: str) -> None:
    console.warn("Snowflake blocked this command because the current role lacks a privilege.")
    if detail:
        console.say(f"Snowflake said: {detail}")
    console.field("Current Snowflake role", role)
    console.say("Send the statements below to the person who holds ACCOUNTADMIN on your account.")
    console.say("Ask them to run them once, then run the same command again.")
    for line in grant_statements(role):
        console.say(line)


def trial_message() -> str:
    return ("this Snowflake account is a trial account. A trial account cannot run "
            "containers (Snowpark Container Services) and its jobs cannot reach the "
            "internet. Convert the account to a paid one in Snowsight under Admin > "
            "Billing & Terms, then run the same command again.")


def run(session: Session, sql: str, file_stream=None) -> list[dict]:
    from snowflake.connector.errors import Error
    try:
        return snowflake_connection.execute(session.conn, sql, file_stream)
    except Error as exc:
        detail = str(getattr(exc, "msg", None) or exc)
        if any(marker in detail.lower() for marker in PERMISSION_MARKERS):
            print_permission_help(session.role, detail)
            raise SystemExit(1) from exc
        if "trial" in detail.lower():
            fail(trial_message())
        console.say(detail)
        fail(f"Snowflake refused `{sql.splitlines()[0][:60]}`; fix the problem above and "
             "run the same command again")


def show(session: Session, kind: str, scope: str = "") -> list[dict]:
    return run(session, f"SHOW {kind} {scope}".rstrip())


def find(session: Session, kind: str, name: str, scope: str = "") -> dict | None:
    short = name.rsplit(".", 1)[-1]
    rows = show(session, kind, f"LIKE {literal(short)} {scope}".rstrip())
    return next((row for row in rows if str(row.get("name", "")).upper() == short.upper()), None)


def schema_exists(session: Session, schema: str) -> bool:
    return (find(session, "DATABASES", DATABASE) is not None
            and find(session, "SCHEMAS", schema, f"IN DATABASE {DATABASE}") is not None)


def app_schemas(session: Session) -> list[str]:
    """Every app schema in the database, by the marker deploy writes."""
    if find(session, "DATABASES", DATABASE) is None:
        return []
    return [str(row["name"]) for row in show(session, "SCHEMAS", f"IN DATABASE {DATABASE}")
            if managed(row) and str(row["name"]).startswith("PDT_")]


def snowflake_settings(app: dict) -> tuple[str, str]:
    account = str(app["platform"].get("account") or os.environ.get("SNOWFLAKE_ACCOUNT") or "")
    user = str(app["platform"].get("user") or os.environ.get("SNOWFLAKE_USER") or "")
    return account.strip(), user.strip()


def choose_value(app: dict, key: str, question: str, env_name: str, check) -> str:
    if not can_ask():
        fail(f"no Snowflake {key} to deploy with; set platform.{key} in pdt.yml, "
             f"or set the {env_name} environment variable")
    while True:
        answer = ask(f"{question} ")
        problem = check(answer) if answer else "This one is needed. Please type a value."
        if problem == "":
            break
        console.warn(problem)
    saved = config.save_platform_key(app, key, answer)
    console.done(f"Saved {key} {answer} to {saved.relative_to(config.find_project())}.")
    return answer


def ensure_session(app: dict, fresh: bool = False) -> Session:
    """A signed-in session. `fresh` skips the cached browser login, for `pdt login`."""
    account, user = snowflake_settings(app)
    if account == "":
        account = choose_value(app, "account",
                               "Which Snowflake account should hold your jobs? (orgname-accountname)",
                               "SNOWFLAKE_ACCOUNT", config.snowflake_account_problem)
    if user == "":
        user = choose_value(app, "user", "Which Snowflake user name do you sign in with?",
                            "SNOWFLAKE_USER", lambda answer: "")
    overrides = {"account": account, "user": user}
    kwargs = snowflake_connection.connect_kwargs(**overrides)
    if kwargs.get("authenticator") == "externalbrowser":
        if not can_ask():
            fail("no Snowflake sign-in for this run; set SNOWFLAKE_PRIVATE_KEY_FILE to a "
                 "key pair file, or SNOWFLAKE_PASSWORD to a programmatic access token, "
                 "with SNOWFLAKE_ACCOUNT and SNOWFLAKE_USER")
        if fresh:
            overrides["client_store_temporary_credential"] = False
            console.status(f"Opening the browser to sign in to Snowflake account {account} "
                           f"as {user}...")
        else:
            console.status(f"Signing in to Snowflake account {account} as {user} "
                           "(a browser opens when no sign-in is cached)...")
    else:
        console.status(f"Signing in to Snowflake account {account} as {user}...")
    try:
        conn = snowflake_connection.connect(**overrides)
    except Exception as exc:  # noqa: BLE001 - the connector raises several types
        fail(f"Snowflake sign-in failed: {exc}")
    facts = snowflake_connection.execute(conn, (
        "SELECT CURRENT_ORGANIZATION_NAME() AS org, CURRENT_ACCOUNT_NAME() AS account, "
        "CURRENT_REGION() AS region, CURRENT_ROLE() AS role, CURRENT_USER() AS user"))[0]
    identifier = f"{facts['org']}-{facts['account']}"
    configured = str(app["platform"].get("account") or "").strip()
    if configured and "-" in configured and configured.lower() != identifier.lower():
        fail(f"configured Snowflake account {configured} does not match the sign-in "
             f"({identifier})")
    region = str(facts["region"] or "").rsplit(".", 1)[-1]
    if not str(app["platform"].get("region") or "").strip():
        saved = config.save_platform_key(app, "region", region)
        console.done(f"Saved region {region} to {saved.relative_to(config.find_project())}.")
    session = Session(conn, identifier, region, str(facts["role"]), str(facts["user"]), False)
    session.has_warehouse = managed(find(session, "WAREHOUSES", WAREHOUSE))
    if session.has_warehouse:
        run(session, f"USE WAREHOUSE {WAREHOUSE}")
    return session


def relogin(app: dict) -> int:
    session = ensure_session(app, fresh=True)
    console.done(f"Signed in as {session.user} with role {session.role}")
    console.field("Account", session.account)
    console.field("Region", session.region)
    return 0


def snow_command(session: Session, app: dict) -> list[str]:
    """`snow` from this script's own environment, signed in the same way as the session."""
    binary = Path(sys.executable).with_name("snow" + (".exe" if os.name == "nt" else ""))
    account, user = snowflake_settings(app)
    kwargs = snowflake_connection.connect_kwargs(account=account or session.account, user=user)
    command = [str(binary), "--temporary-connection", "--account", kwargs["account"],
               "--user", kwargs["user"], "--role", session.role]
    if kwargs.get("private_key_file"):
        command += ["--private-key-file", kwargs["private_key_file"]]
    elif not kwargs.get("password"):
        # The password, when there is one, reaches snow through SNOWFLAKE_PASSWORD.
        command += ["--authenticator", "externalbrowser", "--client-store-temporary-credential"]
    return command


def build_image(session: Session, app: dict, app_names: dict[str, str]) -> None:
    stage = stage_build_context(app)
    try:
        write_dockerfile(stage, app)
        base = snow_command(session, app)
        command = [base[0], "spcs", "service", "remote-build", "--build-context-dir", str(stage),
                   "--location", app_names["repository"], "--name", app["name"],
                   "--image-tag", "latest", *base[1:]]
        if subprocess.run(command, check=False).returncode != 0:
            fail("pdt snow spcs service remote-build failed; fix the problem above and "
                 "run the deploy again")
    finally:
        shutil.rmtree(stage, ignore_errors=True)


def job_spec(app_names: dict[str, str], has_secret: bool, has_store: bool) -> str:
    env = {}
    if has_secret:
        env["PDT_ENV_SECRET_RESOURCE"] = app_names["secret_resource"]
    if has_store:
        env["PDT_STORAGE_URL"] = app_names["store_url"]
    container: dict = {"name": "main", "image": app_names["image"]}
    if env:
        container["env"] = env
    if has_secret:
        container["secrets"] = [{"snowflakeSecret": {"objectName": app_names["secret"]},
                                 "envVarName": "PDT_ENV_JSON",
                                 "secretKeyRef": "secret_string"}]
    spec = {"spec": {"containers": [container],
                     "logExporters": {"eventTableConfig": {"logLevel": "INFO"}}}}
    return yaml.safe_dump(spec, sort_keys=False)


def task_body(app_names: dict[str, str]) -> str:
    # One job service per run, named by its UTC start, submitted without waiting.
    job = (f"EXECUTE JOB SERVICE IN COMPUTE POOL {POOL} NAME = ' || run_name || ' ASYNC = TRUE "
           f"EXTERNAL_ACCESS_INTEGRATIONS = ({INTEGRATION}) COMMENT = ''{MARK}'' "
           f"TAG ({TAG} = ''pdt'') FROM @{app_names['spec_stage']} "
           f"SPECIFICATION_FILE = ''{SPEC_FILE}''")
    return (
        "BEGIN\n"
        f"  LET run_name STRING := '{app_names['schema_fqn']}.{RUN_PREFIX}' "
        "|| TO_VARCHAR(SYSDATE(), 'YYYYMMDD_HH24MISS');\n"
        f"  EXECUTE IMMEDIATE '{job}';\n"
        "END")


def upload_spec(session: Session, app_names: dict[str, str], spec: str) -> None:
    import io
    run(session, f"PUT 'file://{SPEC_FILE}' @{app_names['spec_stage']} AUTO_COMPRESS = FALSE "
                 "OVERWRITE = TRUE", file_stream=io.BytesIO(spec.encode()))


def tag(session: Session, domain: str, name: str) -> None:
    run(session, f"ALTER {domain} {name} SET TAG {TAG} = 'pdt'")


def read_secret(session: Session, app_names: dict[str, str]) -> str | None:
    """The deployed secret through READ_ENV, or None when there is none to read."""
    if not session.has_warehouse or not schema_exists(session, app_names["schema"]):
        return None
    if find(session, "USER FUNCTIONS", app_names["function"],
            f"IN SCHEMA {app_names['schema_fqn']}") is None:
        return None
    rows = run(session, f"SELECT {app_names['function']}() AS value")
    return str(rows[0]["value"]) if rows else None


def write_secret(session: Session, app_names: dict[str, str], payload: str, exists: bool) -> None:
    if exists:
        run(session, f"ALTER SECRET {app_names['secret']} SET SECRET_STRING = {literal(payload)}")
    else:
        run(session, f"CREATE SECRET {app_names['secret']} TYPE = GENERIC_STRING "
                     f"SECRET_STRING = {literal(payload)} COMMENT = '{MARK}'")


def create_reader(session: Session, app_names: dict[str, str]) -> None:
    # SQL cannot read a secret's value; a Python function with the secret bound can.
    run(session, (
        f"CREATE OR REPLACE FUNCTION {app_names['function']}() RETURNS STRING LANGUAGE PYTHON "
        f"RUNTIME_VERSION = 3.12 HANDLER = 'read' EXTERNAL_ACCESS_INTEGRATIONS = ({INTEGRATION}) "
        f"SECRETS = ('env' = {app_names['secret']}) COMMENT = '{MARK}' AS $$\n"
        "import _snowflake\n"
        "def read():\n"
        "    return _snowflake.get_generic_secret_string('env')\n"
        "$$"))


def deployer_store(session: Session, app_name: str) -> Store:
    return Store(names(app_name)["store_url"], session.conn)


def to_utc(value) -> datetime.datetime | None:
    if value is None or value == "":
        return None
    moment = value if isinstance(value, datetime.datetime) else datetime.datetime.fromisoformat(str(value))
    if moment.tzinfo is None:
        return moment.replace(tzinfo=datetime.UTC)
    return moment.astimezone(datetime.UTC)


def service_run(row: dict) -> runs_cli.Run:
    status = str(row.get("status") or "").upper()
    started = to_utc(row.get("created_on"))
    ended = to_utc(row.get("updated_on")) if status in JOB_STATUSES else None
    return runs_cli.Run(str(row["name"]), started, ended, JOB_STATUSES.get(status, "running"))


def event_table(session: Session) -> str:
    rows = run(session, "SHOW PARAMETERS LIKE 'EVENT_TABLE' IN ACCOUNT")
    return str(rows[0].get("value") or "") if rows else ""


def logs_readable(session: Session) -> tuple[str, str]:
    """The event table to read, or the reason it cannot be read."""
    if not session.has_warehouse:
        return "", f"warehouse {WAREHOUSE} is missing; deploy an app first"
    table = event_table(session)
    if table == "":
        return "", ("this Snowflake account has no event table, so job logs go nowhere. Ask "
                    "the ACCOUNTADMIN to run: ALTER ACCOUNT SET EVENT_TABLE = "
                    "SNOWFLAKE.TELEMETRY.EVENTS")
    return table, ""


def log_filter(schema: str, service: str | None = None) -> str:
    where = (f"RECORD_TYPE = 'LOG' AND RESOURCE_ATTRIBUTES:\"snow.database.name\" = '{DATABASE}' "
             f"AND RESOURCE_ATTRIBUTES:\"snow.schema.name\" = '{schema}'")
    if service is not None:
        where += f" AND RESOURCE_ATTRIBUTES:\"snow.service.name\" = '{service}'"
    return where


def exit_codes(session: Session, table: str, schema: str, count: int) -> dict[str, int]:
    rows = run(session, (
        f"SELECT RESOURCE_ATTRIBUTES:\"snow.service.name\"::STRING AS service, "
        f"VALUE::STRING AS message FROM {table} WHERE {log_filter(schema)} "
        f"AND VALUE::STRING LIKE '{runs_cli.EXIT_MARKER}%' "
        f"ORDER BY TIMESTAMP DESC LIMIT {max(count, 1)}"))
    codes = {}
    for row in rows:
        code = runs_cli.exit_code([runs_cli.parse_line(str(row["message"]), None)])
        if row.get("service") and code is not None:
            codes[str(row["service"])] = code
    return codes


def list_runs(session: Session, app_names: dict[str, str]) -> list[runs_cli.Run]:
    if not schema_exists(session, app_names["schema"]):
        return []
    rows = show(session, "JOB SERVICES", f"IN SCHEMA {app_names['schema_fqn']}")
    found = [service_run(row) for row in rows if str(row["name"]).startswith(RUN_PREFIX)]
    found.sort(key=lambda item: item.started, reverse=True)
    table, _ = logs_readable(session)
    if found and table:
        codes = exit_codes(session, table, app_names["schema"], len(found))
        for item in found:
            item.exit_code = codes.get(item.id)
    return found


def read_lines(session: Session, app_names: dict[str, str], run_id: str) -> list[runs_cli.Line]:
    table, problem = logs_readable(session)
    if problem:
        fail(problem)
    rows = run(session, (
        f"SELECT TIMESTAMP, VALUE::STRING AS message FROM {table} "
        f"WHERE {log_filter(app_names['schema'], run_id)} ORDER BY TIMESTAMP"))
    return [runs_cli.parse_line(str(row["message"] or ""), to_utc(row.get("timestamp")))
            for row in rows]


def average_run_seconds(found: list[runs_cli.Run]) -> float | None:
    durations = [(item.ended - item.started).total_seconds()
                 for item in found[:RECENT_RUNS] if item.ended is not None]
    if not durations:
        return None
    return sum(durations) / len(durations)


def rate_sheet(session: Session) -> dict[str, float]:
    """This account's price per credit and per TB, from its rate sheet when it can be read."""
    if not session.has_warehouse:
        return {}
    from snowflake.connector.errors import Error
    view = "SNOWFLAKE.ORGANIZATION_USAGE.RATE_SHEET_DAILY"
    try:
        rows = snowflake_connection.execute(session.conn, (
            f"SELECT RATING_TYPE, EFFECTIVE_RATE FROM {view} WHERE ACCOUNT_NAME = "
            f"CURRENT_ACCOUNT_NAME() AND RATING_TYPE IN ('compute', 'storage') AND DATE = "
            f"(SELECT MAX(DATE) FROM {view} WHERE ACCOUNT_NAME = CURRENT_ACCOUNT_NAME())"))
    except Error:
        return {}
    return {str(row["rating_type"]).lower(): float(row["effective_rate"]) for row in rows}


def prices(session: Session) -> tuple[float, float, str]:
    """(price per credit, price per TB a month, where they come from)."""
    rates = rate_sheet(session)
    if "compute" in rates and "storage" in rates:
        return rates["compute"], rates["storage"], "your account's rate sheet"
    region = session.region if session.region in LIST_PRICES else DEFAULT_LIST_PRICE
    credit, storage = LIST_PRICES[region]
    source = f"{CONSUMPTION_TABLE}, Standard edition on demand in {region}"
    if region != session.region:
        source += f" ({session.region} is not in pdt's table)"
    return credit, storage, source


def cost_estimate(session: Session, cron: str, found: list[runs_cli.Run],
                  store_usage: tuple[int, int] | None) -> CostEstimate:
    console.status("Working out the monthly cost from Snowflake's credit rates...")
    runs = config.runs_per_month(cron)
    seconds = average_run_seconds(found)
    if seconds is None:
        seconds = ASSUMED_RUN_MINUTES * 60
        basis = f"{ASSUMED_RUN_MINUTES:g} min assumed"
    else:
        basis = f"{seconds / 60:.1f} min avg of recent runs"
    credit, storage, source = prices(session)
    pool_cost = runs * (seconds + IDLE_SECONDS) / 3600 * POOL_CREDITS_PER_HOUR * credit
    task_cost = runs * TASK_SECONDS / 3600 * TASK_CREDITS_PER_HOUR * credit
    items = [
        (f"compute pool {POOL} ({INSTANCE_FAMILY}): ~{runs:.0f} runs x {basis} "
         f"+ {IDLE_SECONDS} s idle", pool_cost),
        (f"serverless task: ~{runs:.0f} runs x {TASK_SECONDS:g} s", task_cost),
    ]
    if store_usage is not None:
        count, size = store_usage
        items.append((store_cost_label(count, size), size / 1024 ** 4 * storage))
    return CostEstimate(items, source,
                        f"excludes warehouse {WAREHOUSE} (used only while pdt reads logs and "
                        "prices), image builds, and queries the job itself runs")


def secrets(app: dict, action: str, assume_yes: bool, name: str | None = None) -> int:
    session = ensure_session(app)
    app_names = names(app["name"])
    secret = None
    if schema_exists(session, app_names["schema"]):
        secret = find(session, "SECRETS", app_names["secret"], f"IN SCHEMA {app_names['schema_fqn']}")
        require_managed(secret, f"secret {app_names['secret']}")
    current = read_secret(session, app_names) if secret else None
    if secret is not None and current is None:
        fail(f"warehouse {WAREHOUSE} or function {app_names['function']} is missing, so the "
             f"deployed secret cannot be read. Run `pdt deploy {app['name']}` first.")

    def write(values: dict[str, str]) -> None:
        console.step(f"updating secret {app_names['secret']}")
        write_secret(session, app_names, json.dumps(values, sort_keys=True), True)

    return run_secrets(action, app, current, write, assume_yes, name)


def deploy(app: dict, assume_yes: bool) -> int:
    name = app["name"]
    session = ensure_session(app)
    app_names = names(name)
    cron = config.cron_expression(app["schedule"])
    timezone = app["timezone"]
    values = gather_secrets(app)
    payload = json.dumps(values, sort_keys=True)

    console.status(f"Checking current state in account {session.account} ({session.region})...")
    database = find(session, "DATABASES", DATABASE)
    require_managed(database, f"database {DATABASE}")
    warehouse = find(session, "WAREHOUSES", WAREHOUSE)
    require_managed(warehouse, f"warehouse {WAREHOUSE}")
    pool = find(session, "COMPUTE POOLS", POOL)
    require_managed(pool, f"compute pool {POOL}")
    integration = find(session, "INTEGRATIONS", INTEGRATION)
    require_managed(integration, f"integration {INTEGRATION}")
    data_database = find(session, "DATABASES", DATA_DATABASE)
    require_managed(data_database, f"database {DATA_DATABASE}")
    store = deployer_store(session, name) if app["storage"] else None
    usage = (store.usage() if data_database is not None else (0, 0)) if store else None
    schema = database is not None and schema_exists(session, app_names["schema"])
    scope = f"IN SCHEMA {app_names['schema_fqn']}"
    repository = find(session, "IMAGE REPOSITORIES", app_names["repository"], scope) if schema else None
    require_managed(repository, f"image repository {app_names['repository']}")
    secret = find(session, "SECRETS", app_names["secret"], scope) if schema else None
    require_managed(secret, f"secret {app_names['secret']}")
    task = find(session, "TASKS", app_names["task"], scope) if schema else None
    require_managed(task, f"task {app_names['task']}")
    found = list_runs(session, app_names) if schema else []
    secret_state = None
    if values:
        if secret is None:
            secret_state = "create"
        else:
            secret_state = "unchanged" if read_secret(session, app_names) == payload else "update"

    def verb(row) -> str:
        return "use existing" if row is not None else "create"

    actions = [f"{verb(database)} database {DATABASE}",
               f"{verb(warehouse)} warehouse {WAREHOUSE} (XSMALL, suspends after {IDLE_SECONDS} s)",
               f"{verb(pool)} compute pool {POOL} ({INSTANCE_FAMILY}, 1 node, suspends after "
               f"{IDLE_SECONDS} s)",
               f"{verb(integration)} external access integration {INTEGRATION} (any host, port 443)"]
    if store:
        actions.append(f"{verb(data_database)} stage {DATA_STAGE} (kept after destroy)")
    actions.append(f"{'use existing' if schema else 'create'} schema {app_names['schema_fqn']}")
    actions.append(f"{verb(repository)} image repository {app_names['repository']}")
    actions.append(image_action(app, f"build image {app_names['image']} in Snowflake"))
    if secret_state:
        actions.append(f"{secret_state} secret {app_names['secret']} "
                       f"({len(values)} env vars as one json blob)")
        actions.append(f"{'update' if secret else 'create'} function {app_names['function']} "
                       "(reads the secret back for pdt secrets)")
    actions.append(f"upload the job specification to stage {app_names['spec_stage']}")
    actions.append(f"{'update' if task else 'create'} task {app_names['task']}: "
                   f'"{cron}" ({timezone})')
    cost = cost_estimate(session, cron, found, usage)
    if not confirm(actions, assume_yes, cost):
        console.warn("Aborted; nothing was changed.")
        return 1

    if database is None:
        console.step(f"creating database {DATABASE}")
        run(session, f"CREATE DATABASE {DATABASE} DATA_RETENTION_TIME_IN_DAYS = 0 COMMENT = '{MARK}'")
    shared_scope = f"IN SCHEMA {DATABASE}.PUBLIC"
    if find(session, "TAGS", TAG, shared_scope) is None:
        run(session, f"CREATE TAG {TAG} COMMENT = '{MARK}'")
        tag(session, "DATABASE", DATABASE)
    if find(session, "NETWORK RULES", NETWORK_RULE, shared_scope) is None:
        run(session, f"CREATE NETWORK RULE {NETWORK_RULE} MODE = EGRESS TYPE = HOST_PORT "
                     f"VALUE_LIST = ('0.0.0.0:443') COMMENT = '{MARK}'")
    if warehouse is None:
        console.step(f"creating warehouse {WAREHOUSE}")
        run(session, f"CREATE WAREHOUSE {WAREHOUSE} WAREHOUSE_SIZE = XSMALL AUTO_SUSPEND = "
                     f"{IDLE_SECONDS} AUTO_RESUME = TRUE INITIALLY_SUSPENDED = TRUE COMMENT = '{MARK}'")
        tag(session, "WAREHOUSE", WAREHOUSE)
        session.has_warehouse = True
    run(session, f"USE WAREHOUSE {WAREHOUSE}")
    if pool is None:
        console.step(f"creating compute pool {POOL}")
        run(session, f"CREATE COMPUTE POOL {POOL} MIN_NODES = 1 MAX_NODES = 1 "
                     f"INSTANCE_FAMILY = {INSTANCE_FAMILY} AUTO_SUSPEND_SECS = {IDLE_SECONDS} "
                     f"INITIALLY_SUSPENDED = TRUE COMMENT = '{MARK}'")
        tag(session, "COMPUTE POOL", POOL)
    if integration is None:
        console.step(f"creating external access integration {INTEGRATION}")
        run(session, f"CREATE EXTERNAL ACCESS INTEGRATION {INTEGRATION} ALLOWED_NETWORK_RULES = "
                     f"({NETWORK_RULE}) ALLOWED_AUTHENTICATION_SECRETS = all ENABLED = TRUE "
                     f"COMMENT = '{MARK}'")
    if store and data_database is None:
        console.step(f"creating stage {DATA_STAGE}")
        run(session, f"CREATE DATABASE {DATA_DATABASE} DATA_RETENTION_TIME_IN_DAYS = 0 "
                     f"COMMENT = '{STORE_MARK}'")
        tag(session, "DATABASE", DATA_DATABASE)
    if store and find(session, "STAGES", DATA_STAGE, f"IN SCHEMA {DATA_DATABASE}.PUBLIC") is None:
        run(session, f"CREATE STAGE {DATA_STAGE} ENCRYPTION = (TYPE = 'SNOWFLAKE_SSE') "
                     f"COMMENT = '{STORE_MARK}'")
        tag(session, "STAGE", DATA_STAGE)
    if not schema:
        console.step(f"creating schema {app_names['schema_fqn']}")
        run(session, f"CREATE SCHEMA {app_names['schema_fqn']} COMMENT = '{MARK} pdt-app={name}'")
        tag(session, "SCHEMA", app_names["schema_fqn"])
    if find(session, "STAGES", app_names["spec_stage"], scope) is None:
        run(session, f"CREATE STAGE {app_names['spec_stage']} COMMENT = '{MARK}'")
        tag(session, "STAGE", app_names["spec_stage"])
    if repository is None:
        console.step(f"creating image repository {app_names['repository']}")
        run(session, f"CREATE IMAGE REPOSITORY {app_names['repository']} COMMENT = '{MARK}'")
    console.step(f"building image {app_names['image']}")
    build_image(session, app, app_names)
    if secret_state in ("create", "update"):
        console.step(f"{'creating' if secret is None else 'updating'} secret {app_names['secret']}")
        write_secret(session, app_names, payload, secret is not None)
    if secret_state:
        create_reader(session, app_names)
    console.step(f"uploading the job specification to {app_names['spec_stage']}")
    upload_spec(session, app_names, job_spec(app_names, bool(values), store is not None))
    console.step(f'scheduling {app_names["task"]}: "{cron}" ({timezone})')
    run(session, f"CREATE OR REPLACE TASK {app_names['task']} SCHEDULE = "
                 f"{literal(f'USING CRON {cron} {timezone}')} "
                 "USER_TASK_MANAGED_INITIAL_WAREHOUSE_SIZE = 'XSMALL' "
                 f"COMMENT = '{MARK}' AS\n{task_body(app_names)}")
    tag(session, "TASK", app_names["task"])
    run(session, f"ALTER TASK {app_names['task']} RESUME")
    console.done(f"Deployed {name}.")
    console.field("Run it once", f'pdt snow sql -q "EXECUTE TASK {app_names["task"]}"')
    console.field("Runs", f"pdt runs {name}")
    return 0


def destroy(app: dict, assume_yes: bool) -> int:
    name = app["name"]
    session = ensure_session(app)
    app_names = names(name)

    console.status(f"Checking current state in account {session.account} ({session.region})...")
    database = find(session, "DATABASES", DATABASE)
    schemas = []
    if managed(database):
        schemas = [row for row in show(session, "SCHEMAS", f"IN DATABASE {DATABASE}")
                   if str(row["name"]).upper() not in ("PUBLIC", "INFORMATION_SCHEMA")]
    elif database is not None:
        console.note(f"database {DATABASE} is not managed by PDT; keeping it")
    schema = next((row for row in schemas if str(row["name"]) == app_names["schema"]), None)
    if schema is not None and not managed(schema):
        console.note(f"schema {app_names['schema_fqn']} is not managed by PDT; keeping it")
        schema = None
    jobs = []
    if schema is not None:
        jobs = [str(row["name"]) for row in
                show(session, "JOB SERVICES", f"IN SCHEMA {app_names['schema_fqn']}")]
    others = [str(row["name"]) for row in schemas
              if managed(row) and str(row["name"]) != app_names["schema"]]
    foreign = [str(row["name"]) for row in schemas if not managed(row)]
    # A schema pdt does not own keeps the database, and with it everything shared.
    last = managed(database) and not others and not foreign
    warehouse = find(session, "WAREHOUSES", WAREHOUSE)
    pool = find(session, "COMPUTE POOLS", POOL)
    integration = find(session, "INTEGRATIONS", INTEGRATION)
    data_database = find(session, "DATABASES", DATA_DATABASE)
    store = deployer_store(session, name) if app["storage"] and managed(data_database) else None

    actions = []
    if schema is not None:
        if jobs:
            actions.append(f"delete {len(jobs)} job service(s) of {app_names['schema_fqn']}")
        actions.append(f"delete task {app_names['task']}")
        actions.append(f"delete schema {app_names['schema_fqn']} (secret, function, image "
                       "repository, and specification stage)")
    shared = [("integration", INTEGRATION, integration), ("compute pool", POOL, pool),
              ("warehouse", WAREHOUSE, warehouse), ("database", DATABASE, database)]
    for kind, label, row in shared:
        if last and managed(row):
            actions.append(f"delete {kind} {label} (no other apps use it)")
    kept = [f"schema {DATABASE}.{item}" for item in foreign]
    for kind, label, row in shared:
        if row is not None and not (last and managed(row)):
            kept.append(f"{kind} {label}")
    if store is not None:
        kept.append(store_kept_line(f"stage {DATA_STAGE}", store.usage()[0], name))
    elif data_database is not None:
        kept.append(f"database {DATA_DATABASE}")
    if not actions:
        console.done(f"Nothing to remove for {name} in account {session.account}.")
        if others:
            console.note(f"PDT apps still deployed: {', '.join(others)}.")
        if kept:
            console.heading("Still present:")
            for resource in kept:
                console.bullet(resource)
        return 0
    if store is not None:
        warn_if_locked(store, name)
    if not confirm(actions, assume_yes):
        console.warn("Aborted; nothing was changed.")
        return 1

    if schema is not None:
        console.step(f"deleting task {app_names['task']}")
        run(session, f"ALTER TASK IF EXISTS {app_names['task']} SUSPEND")
        run(session, f"DROP TASK IF EXISTS {app_names['task']}")
        for job in jobs:
            console.step(f"deleting job service {job}")
            run(session, f"DROP SERVICE IF EXISTS {app_names['schema_fqn']}.{job}")
        console.step(f"deleting schema {app_names['schema_fqn']}")
        run(session, f"DROP SCHEMA IF EXISTS {app_names['schema_fqn']}")
    if last:
        if managed(integration):
            console.step(f"deleting integration {INTEGRATION}")
            run(session, f"DROP INTEGRATION IF EXISTS {INTEGRATION}")
        if managed(pool):
            console.step(f"deleting compute pool {POOL}")
            run(session, f"ALTER COMPUTE POOL {POOL} STOP ALL")
            run(session, f"DROP COMPUTE POOL IF EXISTS {POOL}")
        if managed(warehouse):
            console.step(f"deleting warehouse {WAREHOUSE}")
            run(session, f"DROP WAREHOUSE IF EXISTS {WAREHOUSE}")
        console.step(f"deleting database {DATABASE}")
        run(session, f"DROP DATABASE IF EXISTS {DATABASE}")
    console.done(f"Removed {name} from account {session.account}.")
    if others:
        console.note(f"PDT apps still deployed: {', '.join(others)}.")
    kept += leftovers(session)
    if kept:
        console.heading("Still present:")
        for resource in dict.fromkeys(kept):
            console.bullet(resource)
    elif not others:
        console.done("Nothing remains.")
    return 0


def leftovers(session: Session) -> list[str]:
    """Every PDT-named object still in the account, after a destroy."""
    found = []
    for row in show(session, "DATABASES", f"LIKE '{DATABASE}%'"):
        if str(row["name"]) != DATA_DATABASE:
            found.append(f"database {row['name']}")
    for kind, label, name in (("COMPUTE POOLS", "compute pool", POOL),
                              ("INTEGRATIONS", "integration", INTEGRATION),
                              ("WAREHOUSES", "warehouse", WAREHOUSE)):
        if find(session, kind, name) is not None:
            found.append(f"{label} {name}")
    return found


def storage(app: dict, rest: list[str], assume_yes: bool) -> int:
    session = ensure_session(app)
    return storage_cli.run(deployer_store(session, app["name"]), app, rest, assume_yes)


def runs(app: dict, rest: list[str]) -> int:
    session = ensure_session(app)
    app_names = names(app["name"])
    return runs_cli.runs(lambda: list_runs(session, app_names), app["name"], rest)


def logs(app: dict, rest: list[str]) -> int:
    session = ensure_session(app)
    app_names = names(app["name"])
    return runs_cli.logs(lambda: list_runs(session, app_names),
                         lambda item: read_lines(session, app_names, item.id), app["name"], rest)


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == "snow":
        binary = Path(sys.executable).with_name("snow" + (".exe" if os.name == "nt" else ""))
        return subprocess.run([str(binary), *sys.argv[2:]]).returncode
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command",
                        choices=("deploy", "destroy", "login", "storage", "secrets", "runs", "logs"))
    parser.add_argument("app")
    parser.add_argument("rest", nargs="*")
    parser.add_argument("--yes", action="store_true")
    args = parser.parse_intermixed_args()
    try:
        app = config.merged_app(args.app)
    except config.ConfigError as exc:
        fail(str(exc))
    config.load_env(app["dir"])
    if args.command == "login":
        return relogin(app)
    if args.command == "storage":
        return storage(app, args.rest, args.yes)
    if args.command == "runs":
        return runs(app, args.rest)
    if args.command == "logs":
        return logs(app, args.rest)
    if args.command == "secrets":
        return secrets(app, args.rest[0], args.yes, *args.rest[1:])
    if args.command == "deploy":
        return deploy(app, args.yes)
    return destroy(app, args.yes)


if __name__ == "__main__":
    sys.exit(main())
