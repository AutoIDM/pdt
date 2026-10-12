#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "azure-identity",
#     "azure-keyvault-secrets",
#     "boto3",
#     "google-auth",
#     "google-auth-oauthlib",
#     "google-cloud-secret-manager",
#     "msal",
#     "python-dotenv",
#     "pyyaml",
#     "rich",
# ]
# ///
"""Send notices for failed pdt jobs."""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from threading import Event, Thread

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from pdt import config
from pdt.utils.log import log
from pdt.utils.send_email import email_problems, send_email

TIMEOUT = 10
ATTEMPTS = 2


def notice_text(app: str, provider: str, exit_code: int, started: str, ended: str,
                duration: float) -> str:
    # pdt runs takes the start from the provider, which records it before the image
    # download and container start, so the window begins 5 minutes before the container
    # start and lasts 10 minutes.
    since = datetime.fromisoformat(started.removesuffix("Z") + "+00:00") - timedelta(minutes=5)
    return (f"App: {app}\nProvider: {provider}\n\n"
            f"The job started at {started} and failed with exit code {exit_code} "
            f"after {duration:.0f} seconds.\nIt ended at {ended}.\n\n"
            f"To read its log:\n"
            f"pdt logs {app} --failed --since {since:%Y-%m-%dT%H:%MZ} --span 10m\n")


def send_with_retry(from_addr: str, to, subject: str, body: str) -> str:
    """"" once the email is sent, else why the last attempt failed."""
    reason = ""
    for _ in range(ATTEMPTS):
        done = Event()
        error = []

        def send() -> None:
            try:
                send_email(from_addr, to, subject, body)
            except (Exception, SystemExit) as exc:
                error.append(exc)
            finally:
                done.set()

        Thread(target=send, daemon=True).start()
        if not done.wait(TIMEOUT):
            reason = f"the email server did not answer within {TIMEOUT} seconds"
        elif not error:
            return ""
        elif isinstance(error[0], SystemExit):
            reason = "the send failed; the ERROR line above names the cause"
        else:
            reason = str(error[0]) or type(error[0]).__name__
    return reason


def unsent_reason(app: dict) -> str:
    """Why a failed run of the app sends no email, or "" when it sends one."""
    if "email" not in app["on_failure"]:
        return "on_failure in config.yml does not list email"
    if not config.sends_failure_email(app):
        return "notify.email.to is not set in config.yml"
    return "; ".join(email_problems(app["config"], failure_email=True))


def notify_failure(app: dict, exit_code: int, started: str,
                   ended: str, duration: float) -> None:
    if exit_code == 0:
        return
    try:
        reason = unsent_reason(app)
        if reason == "":
            body = notice_text(app["name"], app["platform"].get("provider", ""),
                               exit_code, started, ended, duration)
            reason = send_with_retry(str(app["config"]["email_from"]).strip(),
                                     app["notify"]["email"]["to"],
                                     f"pdt: {app['name']} failed", body)
    except Exception as exc:
        reason = str(exc) or type(exc).__name__
    if reason != "":
        log("warning", f"failure notice not sent: {reason}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--app-dir", required=True)
    parser.add_argument("--exit-code", type=int, required=True)
    parser.add_argument("--started", required=True)
    parser.add_argument("--ended", required=True)
    args = parser.parse_args()
    app_dir = Path(args.app_dir).resolve()
    os.environ["PDT_PROJECT"] = str(config.find_project(app_dir))
    app = config.merged_app(app_dir.name)
    config.load_env(app["dir"])
    start = datetime.fromisoformat(args.started.removesuffix("Z") + "+00:00")
    end = datetime.fromisoformat(args.ended.removesuffix("Z") + "+00:00")
    notify_failure(app, args.exit_code,
                   args.started, args.ended, (end - start).total_seconds())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
