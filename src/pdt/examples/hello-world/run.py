#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["pdt-cli==PDT_VERSION"]
# ///
"""Sample hello world app. Use as a template to create your own app.

Run it with `pdt run hello-world`. Edit `main` to do the real work, add
what you need to the dependencies above, and list any env vars in
config.yml so `pdt validate` checks them before a deploy.
"""

from __future__ import annotations

import sys
from pathlib import Path

from pdt.config import ConfigError, load_env, merged_app, missing_env
from pdt.utils.log import die, log

EXIT_OK = 0
EXIT_CONFIG = 1


def main() -> int:
    app_dir = Path(__file__).resolve().parent
    try:
        app = merged_app(app_dir.name)
    except ConfigError as exc:
        die(EXIT_CONFIG, "config is not valid", problem=str(exc))
    load_env(app_dir)
    missing = missing_env(app)
    if missing != "":
        die(EXIT_CONFIG, f"env vars missing: {missing}")

    log("info", app["config"].get("greeting", "Hello, world!"), app=app["name"])
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
