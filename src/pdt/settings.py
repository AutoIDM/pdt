"""pdt's settings for this computer, and the `pdt settings` command.

They live in settings.yml in the user's data folder, not in a project,
because they belong to the person at the keyboard, not to the jobs.
"""

from __future__ import annotations

import os
import tempfile
import uuid
from pathlib import Path

import yaml

from pdt import config, console

HEADER = "# pdt's settings for this computer. Run `pdt settings` to change them.\n"
DEFAULTS = {"usage_stats": True, "install_id": None}
SETTINGS = {
    "usage-stats": ("usage_stats",
                    "send anonymous usage stats: which command ran, whether it worked, "
                    "and how long it took"),
}


def path() -> Path:
    return config.data_home() / "pdt" / "settings.yml"


def load() -> dict:
    try:
        data = yaml.safe_load(path().read_text())
    except (OSError, yaml.YAMLError):
        data = None
    return {**DEFAULTS, **(data if isinstance(data, dict) else {})}


def save(values: dict) -> None:
    values = {**values, "install_id": values.get("install_id") or str(uuid.uuid4())}
    target = path()
    target.parent.mkdir(parents=True, exist_ok=True)
    # Replace the file in one step, so a crash cannot leave half a file.
    with tempfile.NamedTemporaryFile("w", dir=target.parent, suffix=".tmp",
                                     delete=False) as tmp:
        tmp.write(HEADER + yaml.safe_dump(values, sort_keys=False))
    os.replace(tmp.name, target)


def do_not_track() -> bool:
    return os.environ.get("DO_NOT_TRACK", "").strip() not in ("", "0")


def run(name: str | None, value: str | None) -> int:
    if name is None or value is None:
        return show()
    key, _ = SETTINGS[name]
    save({**load(), key: value == "on"})
    console.done(f"{name} is now {value}.")
    if key == "usage_stats" and value == "on" and do_not_track():
        console.note("DO_NOT_TRACK is set, so pdt still sends no usage stats.")
    return 0


def show() -> int:
    values = load()
    for name, (key, _) in SETTINGS.items():
        shown = "on" if values[key] else "off"
        if key == "usage_stats" and do_not_track():
            shown = "off (DO_NOT_TRACK is set)"
        console.field(name, shown)
    console.field("settings file", str(path()))
    return 0
