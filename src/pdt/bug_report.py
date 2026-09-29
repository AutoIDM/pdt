"""Offer a bug report when pdt stops on an error it did not expect.

An uncaught exception is a pdt bug: a mistake the user can fix raises
ConfigError or calls deploy_common.fail, and neither reaches the hook.
The hook prints the usual traceback, then builds a report with private
data removed, saves it to the user's data folder, and offers to open a
prefilled new-issue page on GitHub. Nothing is sent until the user
presses Submit there.
"""

from __future__ import annotations

import getpass
import platform
import re
import socket
import sys
import traceback
import webbrowser
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote_plus, urlencode

from dotenv import dotenv_values

from pdt import __version__, config, console, deploy_common
from pdt.utils.email_auth import can_prompt

ISSUE_URL = "https://github.com/AutoIDM/pdt/issues/new"
MAX_URL = 7000
MAX_LINE = 1000
TRACE_START = "```text\n"
TRACE_END = "\n```\n"
CUT = "... earlier lines removed ..."
# Platform values that name no person or account, so a report keeps them.
PUBLIC_PLATFORM_KEYS = {"provider", "region"}
# Command-line words that are fixed choices, not the user's own values.
PUBLIC_WORDS = set(deploy_common.SECRET_ACTIONS)
SITE_PACKAGES = re.compile(r"[^\s\"'(]*site-packages[/\\]")
TOKEN = "[A-Za-z0-9+/=_-]"
PATTERNS = (
    (re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+"), "<email>"),
    (re.compile(r"\b[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}\b"), "<id>"),
    (re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"), "<ip>"),
    (re.compile(r"(?<!\d)\d{12}(?!\d)"), "<account>"),
    (re.compile(rf"(?={TOKEN}*[A-Za-z])(?={TOKEN}*\d){TOKEN}{{32,}}"), "<secret>"),
)
QUESTION = ("pdt hit an unexpected error. Open a GitHub page to report it? "
            "You can read and edit the report before you send it.")


def install() -> None:
    sys.excepthook = hook


def hook(exc_type, exc, tb) -> None:
    sys.__excepthook__(exc_type, exc, tb)
    if issubclass(exc_type, KeyboardInterrupt):
        return
    try:
        offer(*build_report(exc, tb))
    except (Exception, KeyboardInterrupt):
        pass


def scrub(text: str, secrets: list[str]) -> str:
    for secret in sorted({s for s in secrets if s and len(s) >= 4}, key=len, reverse=True):
        text = text.replace(secret, "<redacted>")
    text = SITE_PACKAGES.sub("<site-packages>/", text)
    try:
        text = text.replace(str(config.find_project()), "<project>")
    except config.ConfigError:
        pass
    home = str(Path.home())
    text = text.replace(home, "~").replace(home.replace("/", "\\"), "~")
    for pattern, label in PATTERNS:
        text = pattern.sub(label, text)
    try:
        user = getpass.getuser()
    except Exception:
        # A container user with no passwd entry makes getuser raise.
        user = ""
    host = socket.gethostname()
    for word, label in ((user, "<user>"), (host, "<host>"), (host.split(".")[0], "<host>")):
        if word != "":
            text = re.sub(rf"(?<![\w-]){re.escape(word)}(?![\w-])", label, text)
    return text


def known_secrets() -> list[str]:
    found: list[str] = []
    try:
        project = config.find_project()
        names = config.app_folders()
        found.extend(names)
        for folder in (project, *(project / name for name in names)):
            for path in config.find_env_files(folder):
                found.extend(value for value in dotenv_values(path).values() if value)
        root = config.load_yaml(project / config.PROJECT_FILE)
        sections = [root.get("platform"),
                    *(entry.get("platform") for entry in root.get("apps") or []
                      if isinstance(entry, dict)),
                    *(config.load_yaml(project / name / config.APP_FILE).get("platform")
                      for name in names)]
        for section in sections:
            if isinstance(section, dict):
                found.extend(value for key, value in section.items()
                             if key not in PUBLIC_PLATFORM_KEYS and isinstance(value, str))
    except Exception:
        pass
    return found


def command_line(args: list[str]) -> str:
    words = []
    for index, word in enumerate(args):
        if word.startswith("-"):
            flag, equals, _ = word.partition("=")
            words.append(f"{flag}=<value>" if equals else flag)
        elif index == 0 or word in PUBLIC_WORDS:
            words.append(word)
        else:
            words.append("<value>")
    return " ".join(["pdt", *words])


def build_report(exc: BaseException, tb) -> tuple[str, str]:
    args = sys.argv[1:]
    try:
        provider = str(config.merged_app(args[1])["platform"].get("provider", "not set"))
    except Exception:
        provider = "unknown"
    clone = (Path(__file__).resolve().parents[2] / "pyproject.toml").is_file()
    trace = "".join(traceback.format_exception(type(exc), exc, tb)).rstrip("\n")
    trace = "\n".join(line[:MAX_LINE] for line in trace.splitlines())
    body = (
        "Please describe what you were doing when this happened:\n\n\n\n"
        f"- Command: `{command_line(args)}`\n"
        f"- pdt version: {__version__}\n"
        f"- Installed from: {'a clone of the repository' if clone else 'a package'}\n"
        f"- Python: {platform.python_version()}\n"
        f"- System: {platform.system()} {platform.release()} {platform.machine()}\n"
        f"- Provider: {provider}\n\n"
        f"{TRACE_START}{trace}{TRACE_END}"
    )
    title = f"{' '.join(['pdt', *args[:1]])}: {type(exc).__name__}"
    secrets = known_secrets()
    return scrub(title, secrets), scrub(body, secrets)


def issue_url(title: str, body: str) -> str:
    """The new-issue address, with the oldest traceback lines cut to fit a browser."""
    head, _, trace = body.partition(TRACE_START)
    lines = trace.removesuffix(TRACE_END).splitlines()
    empty = urlencode({"title": title, "body": head + TRACE_START + CUT + TRACE_END})
    room = MAX_URL - len(f"{ISSUE_URL}?{empty}")
    kept = []
    for line in reversed(lines):
        room -= len(quote_plus(line + "\n"))
        if room < 0 and kept:
            kept.append(CUT)
            break
        kept.append(line)
    body = head + TRACE_START + "\n".join(reversed(kept)) + TRACE_END
    return f"{ISSUE_URL}?{urlencode({'title': title, 'body': body})}"


def save(title: str, body: str) -> Path:
    folder = config.data_home() / "pdt" / "bug-reports"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}.md"
    path.write_text(f"# {title}\n\n{body}", encoding="utf-8")
    return path


def offer(title: str, body: str) -> None:
    path = save(title, body)
    console.say()
    if can_prompt(None) and console.confirm(QUESTION):
        url = issue_url(title, body)
        webbrowser.open(url)
        console.say("If your browser did not open, copy this address into it:")
        console.command(url)
        return
    console.say("pdt hit an unexpected error. It saved a report, with private data removed, to:")
    console.command(str(path))
    console.say("To report the error, open this page and paste the report into it:")
    console.command(ISSUE_URL)
