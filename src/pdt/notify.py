#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "boto3",
#     "google-auth",
#     "google-auth-oauthlib",
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
from pdt.utils.send_email import pick_transport, send_email

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


def send_with_retry(from_addr: str, to, subject: str, body: str) -> bool:
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
        if done.wait(TIMEOUT):
            if not error:
                return True
    return False


def notify_failure(app: dict, exit_code: int, started: str,
                   ended: str, duration: float) -> None:
    if exit_code == 0 or "email" not in app["on_failure"]:
        return
    email = app["notify"].get("email") or {}
    to = email.get("to")
    if to is None or (isinstance(to, str) and to.strip() == "") or to == []:
        return
    try:
        if pick_transport() == "stdout":
            return
        from_addr = str(app["config"].get("email_from") or "").strip()
        if from_addr == "":
            return
        body = notice_text(app["name"], app["platform"].get("provider", ""),
                           exit_code, started, ended, duration)
        send_with_retry(from_addr, to, f"pdt: {app['name']} failed", body)
    except (Exception, SystemExit):
        return


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
