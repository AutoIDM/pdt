#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "pyyaml",
#     "rich",
#     "python-dotenv",
#     "msal",
#     "google-auth",
#     "google-auth-oauthlib",
#     "backoff",
#     "rich-argparse>=1.8.0",
#     "argcomplete",
#     "shellingham",
#     "django>=5.2",
#     "croniter",
#     "tzlocal",
#     "truststore",
# ]
# ///
"""The `pdt gui` server process.

`pdt gui` starts this script through `uv run --script`, so Django lives in
the script's own environment. It serves the project named by PDT_PROJECT
(or the nearest pdt.yml) on localhost and runs its migrations first, so a
new pdt version upgrades the project's .pdt/gui.sqlite3 on start. It
also fetches the DuckDB-WASM files of the SQL workbench (see
`duckdb_wasm`) once, in a thread, so a first start does not wait for them.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from pdt.utils.web import trust_os_certificates
trust_os_certificates()
from pdt import config, duckdb_wasm, gui_cli


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=config.GUI_PORT)
    args = parser.parse_args()
    os.environ["PDT_PROJECT"] = str(config.find_project())
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "pdt.gui.settings")
    import django
    django.setup()
    from django.core.management import call_command
    from django.core.servers.basehttp import get_internal_wsgi_application, run
    call_command("migrate", verbosity=0)
    duckdb_wasm.fetch_in_background()
    from pdt.gui import worker
    worker.start()
    try:
        run("127.0.0.1", args.port, get_internal_wsgi_application(), threading=True)
    except KeyboardInterrupt:
        pass
    except OSError as e:
        logging.getLogger("pdt.gui").error("could not listen on port %s: %s", args.port, e)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
