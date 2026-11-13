"""Deprecated: send an email from an app. Use `pdt.utils.alert.emit`.

`send_email` emits the event `email:send` with the subject as `title`, the body
as `text`, and `html`. Each email entry of the app's `alerts:` list that
receives `email:send` sends it to `to_addrs`, from the entry's own `from`, so
`from_addr` is not used. An email entry receives `email:send` only when its
`events` list holds that name (or `all`); `pdt alerts setup` does not add it.
With no such entry, the console prints the alert line
and no email goes out. Both functions return the method of the first email
entry that receives the event, or "stdout" when none does. A failure is a
warning in the log and never ends the app.
"""

from __future__ import annotations

import warnings
from pathlib import Path

from pdt import config, email_alert, lifecycle
from pdt.utils import alert
from pdt.utils.log import log

EVENT = "email:send"


def _method() -> str:
    alerts = lifecycle.alerts_for(config.running_app_dir().name)
    for entry, _unhandled in lifecycle.receivers(alerts, EVENT):
        if entry["type"] == "email":
            return entry["method"]
    return "stdout"


def pick_transport(env_file: Path | None = None) -> str:
    warnings.warn("pdt.utils.send_email.pick_transport is deprecated; send an alert with "
                  "pdt.utils.alert.emit", DeprecationWarning, stacklevel=2)
    return _method()


def send_email(from_addr: str, to_addrs, subject: str, body: str, html: str = "") -> str:
    warnings.warn("pdt.utils.send_email.send_email is deprecated; send an alert with "
                  "pdt.utils.alert.emit. This call sends an email only when an email entry "
                  "under alerts: in pdt.yml has email:send in its events",
                  DeprecationWarning, stacklevel=2)
    try:
        alert.emit(EVENT, title=subject, text=body, html=html,
                   to=email_alert.addresses(to_addrs))
        return _method()
    except Exception as e:  # noqa: BLE001 - an alert never ends the app
        log("warning", "email not sent", error=str(e))
        return "stdout"
