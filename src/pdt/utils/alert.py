"""Send an alert from an app's run.py.

    from pdt.utils import alert
    alert.emit("compliance:changed", title=subject, text=text, html=page, url=location)

`emit` gives one event to every entry of the app's `alerts:` list that receives
its name (see pdt.lifecycle). `title`, `text`, `html`, and `url` are the content
of the alert: Slack and Teams show the title, the text, and an "Open" button for
the url; the email type sends the title as the subject, the text as the plain
body, and the html as the HTML body. Every other keyword is a labelled field.
The event also carries the project, app, provider, and run_id of this run.

`emit` returns at once. pdt waits for the deliveries when the app's process
ends, for at most lifecycle.FLUSH_SECONDS, so the app calls nothing else. A
delivery that fails prints a warning and the alert line, and never changes the
app's exit code.
"""

from __future__ import annotations

import atexit

from pdt import config, lifecycle
from pdt.utils import storage

_waits_at_exit = False


def emit(name: str, title: str = "", text: str = "", html: str = "", url: str = "",
         **fields: object) -> None:
    global _waits_at_exit
    app = config.running_app_dir().name
    try:
        provider = config.merged_app(app)["platform"].get("provider", "")
    except config.ConfigError:
        provider = ""
    content = {"title": title, "text": text, "html": html, "url": url}
    if not _waits_at_exit:
        atexit.register(lifecycle.flush)
        _waits_at_exit = True
    lifecycle.emit(name, **lifecycle.app_params(app, provider), run_id=storage.RUN_ID,
                   **{key: value for key, value in content.items() if value != ""}, **fields)
