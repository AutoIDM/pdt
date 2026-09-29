"""Anonymous usage stats: one event per command, never anything about the user's apps.

pdt sends nothing until the settings file exists, and only an interactive
run creates it, right after it prints the notice. So a person has seen the
notice on every computer that sends, and a build server sends nothing
until someone runs pdt there by hand.
"""

from __future__ import annotations

import json
import os
import platform
import sys
import urllib.request

from pdt import __version__, config, console, settings
from pdt.utils.email_auth import can_prompt

USAGE_URL = "https://us.i.posthog.com/i/v0/e/"
# The PostHog project API key. While it is empty, pdt sends nothing.
USAGE_KEY = ""


def notice(command: str) -> None:
    if (USAGE_KEY == "" or command == "settings" or settings.do_not_track()
            or settings.path().exists() or not can_prompt(None)):
        return
    console.note("pdt sends anonymous usage stats: which command ran, whether it worked, "
                 "how long it took, the cloud provider, the pdt version, and the operating "
                 "system. It sends nothing about your apps, files, or accounts. "
                 "To turn it off, run:")
    console.command("pdt settings usage-stats off")
    try:
        settings.save(settings.load())
    except OSError:
        pass


def app_provider(args) -> str | None:
    app = getattr(args, "app", None)
    try:
        if app in config.find_apps():
            return config.merged_app(app)["platform"].get("provider")
    except Exception:
        pass
    return None


def record(args, exit_code: int, seconds: float) -> None:
    try:
        if USAGE_KEY == "" or settings.do_not_track() or not settings.path().exists():
            return
        values = settings.load()
        if not values["usage_stats"]:
            return
        event = {
            "api_key": USAGE_KEY,
            "event": "pdt command",
            "distinct_id": values["install_id"],
            "properties": {
                "command": args.command,
                "exit_code": exit_code,
                "seconds": round(seconds, 1),
                "provider": app_provider(args),
                "pdt_version": __version__,
                "python_version": f"{sys.version_info.major}.{sys.version_info.minor}",
                "os": platform.system(),
                "arch": platform.machine(),
                "ci": os.environ.get("CI", "").strip() != "",
                "$process_person_profile": False,
            },
        }
        request = urllib.request.Request(
            USAGE_URL, data=json.dumps(event).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=2):
            pass
    except Exception:
        pass
