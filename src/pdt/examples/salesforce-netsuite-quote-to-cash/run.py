#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["pdt-cli[apps]==PDT_VERSION", "meltano"]
# ///
"""Turn Closed Won Salesforce opportunities into NetSuite sales orders and invoices.

This folder is a complete Meltano project. tap-salesforce reads Account,
Contact, Opportunity, OpportunityLineItem and Product2, target-postgres stages
them, dbt and autoidm-transform build the desired NetSuite state,
target-netsuite writes the difference back, and target-salesforce writes the
new NetSuite ids onto the Opportunity. README.md describes the four Meltano
jobs and how to run them by hand.

Needs a Salesforce user with API access, a NetSuite integration record with
REST Web Services and OAuth 2.0 client credentials, and a Postgres database
for the staging tables.

Env (creds only -- put these in .env at this folder or any parent):
  Always:
    TAP_NETSUITE_ACCOUNT_ID, TAP_NETSUITE_CLIENT_ID,
    TAP_NETSUITE_CERTIFICATE_ID, TAP_NETSUITE_PRIVATE_KEY
    TARGET_NETSUITE_ACCOUNT_ID, TARGET_NETSUITE_CLIENT_ID,
    TARGET_NETSUITE_CERTIFICATE_ID, TARGET_NETSUITE_PRIVATE_KEY
    POSTGRES_SQLALCHEMY_URL_NO_DB
  One of, for the read and the write both:
    TAP_SALESFORCE_USERNAME + TAP_SALESFORCE_PASSWORD + TAP_SALESFORCE_SECURITY_TOKEN
      + the same three TARGET_SALESFORCE_ names
    TAP_SALESFORCE_CLIENT_ID + TAP_SALESFORCE_CLIENT_SECRET + TAP_SALESFORCE_REFRESH_TOKEN
      + the same three TARGET_SALESFORCE_ names
  See env.template for the Postgres and notification names as well.

Exit codes: 0 ok, 1 bad config, 2 meltano install failure, 3 meltano run
failure.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

from pdt.config import ConfigError, check_env, load_env, merged_app
from pdt.utils.log import die, log

EXIT_OK = 0
EXIT_CONFIG = 1
EXIT_INSTALL = 2
EXIT_RUN = 3

MELTANO = shutil.which("meltano", path=str(Path(sys.executable).parent)) or "meltano"
INSTALL_ARGS = ["install"]
RUN_ARGS = ["run", "--force", "extract", "transform", "load"]
FIELD_ENV = {
    "NETSUITE_CUSTOMER_ID_FIELD": "netsuite_customer_id_field",
    "NETSUITE_SALES_ORDER_ID_FIELD": "netsuite_sales_order_id_field",
    "NETSUITE_INVOICE_ID_FIELD": "netsuite_invoice_id_field",
}


def meltano(app_dir: Path, args: list[str], environment: str, fields: dict[str, str]) -> int:
    child_env = dict(os.environ)
    child_env["MELTANO_ENVIRONMENT"] = environment
    child_env.update(fields)
    log("info", "starting meltano", args=" ".join(args), environment=environment)
    finished = subprocess.run([MELTANO, *args], cwd=app_dir, env=child_env, check=False)
    return finished.returncode


def main() -> int:
    app_dir = Path(__file__).resolve().parent
    try:
        env_files = load_env(app_dir)
    except ConfigError as e:
        die(EXIT_CONFIG, "bad env", error=str(e))
    for path in env_files:
        log("info", "loaded env file", path=str(path))

    try:
        app = merged_app(app_dir.name)
    except ConfigError as e:
        die(EXIT_CONFIG, "config error", error=str(e))
    problems = check_env(app["env"])
    if problems:
        die(EXIT_CONFIG, "env vars missing", problems="; ".join(problems))
    environment = str(app["config"].get("meltano_environment", "") or "").strip()
    if environment == "":
        die(EXIT_CONFIG, "config.yml missing key", key="meltano_environment")
    fields = {}
    for name, key in FIELD_ENV.items():
        value = str(app["config"].get(key, "") or "").strip()
        if value == "":
            die(EXIT_CONFIG, "config.yml missing key", key=key)
        fields[name] = value

    # `meltano install` runs at every start because pdt's Dockerfile has no build
    # hook for it, so on a container provider every cold start pays that cost.
    code = meltano(app_dir, INSTALL_ARGS, environment, fields)
    if code != 0:
        die(EXIT_INSTALL, "meltano install failed", code=code)

    code = meltano(app_dir, RUN_ARGS, environment, fields)
    if code != 0:
        die(EXIT_RUN, "meltano run failed", code=code)

    log("info", "sync complete", environment=environment)
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
