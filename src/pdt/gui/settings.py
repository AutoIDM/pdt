"""Django settings for `pdt gui`.

The server listens on localhost only and has one user: whoever is at the
keyboard. There is no login. The database is SQLite in the project's
.pdt folder, so a project's run history stays with the project and on
this machine. Without PDT_PROJECT (the tests) the database is in memory.
"""

from __future__ import annotations

import os
import secrets
from pathlib import Path

from tzlocal import get_localzone

from pdt import gui_cli

PROJECT = os.environ.get("PDT_PROJECT", "").strip()
STATE = gui_cli.state_dir(Path(PROJECT)) if PROJECT else None


def secret_key() -> str:
    """One key per project, kept in .pdt, so a cookie survives a restart."""
    if STATE is None:
        return "pdt-gui-tests"
    path = STATE / "gui.secret"
    if not path.is_file():
        path.write_text(secrets.token_urlsafe(48))
    return path.read_text().strip()


SECRET_KEY = secret_key()
DEBUG = False
ALLOWED_HOSTS = ["127.0.0.1", "localhost"]
INSTALLED_APPS = ["django.contrib.messages", "pdt.gui"]
MIDDLEWARE = [
    "pdt.gui.timing.TimingMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
]
ROOT_URLCONF = "pdt.gui.urls"
TEMPLATES = [{
    "BACKEND": "django.template.backends.django.DjangoTemplates",
    "APP_DIRS": True,
    "OPTIONS": {"context_processors": [
        "django.template.context_processors.request",
        "django.contrib.messages.context_processors.messages",
    ]},
}]
MESSAGE_STORAGE = "django.contrib.messages.storage.cookie.CookieStorage"
# The worker thread and a page can write at the same moment; SQLite makes
# one of them wait, and this is how long it may.
DATABASES = {"default": {
    "ENGINE": "django.db.backends.sqlite3",
    "NAME": STATE / "gui.sqlite3" if STATE else ":memory:",
    "OPTIONS": {"timeout": 30},
}}
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
# Pages show the same local times `pdt runs` prints.
TIME_ZONE = str(get_localzone())
USE_TZ = True
