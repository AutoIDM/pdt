from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import traceback
import webbrowser
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import urlencode

from pdt import coding_agents, config, console, watch
from pdt.utils.email_auth import can_prompt

ISSUES_URL = "https://github.com/AutoIDM/pdt/issues/new"
# GitHub sends a person who is not signed in to its login page with the whole link in
# return_to. Measured on 2026-10-19: that page fails from about 4,600 characters of link,
# and from about 5,800 it drops the link, so the report would be lost after the login.
MAX_URL = 4000
AGENT_SECONDS = 120
VALIDATE_SECONDS = 120
REPORTED = ("error", "unhandled")
INTRO = ("`pdt support` wrote this report. It holds only the structure of the error: no "
         "error message, no name, and no value. `<value>` stands for a word the user typed.")
AGENT_INTRO = ("`pdt support` wrote the first part of this report, with only the structure of "
               "the error. {agent}, a coding agent, wrote the part under What failed, and "
               "removed the secrets and private details it found. The user read the whole "
               "report before sending it.")
CUT = "pdt cut the report here to fit the link. Paste the rest of it below."
REQUEST = (
    "A user of pdt, a command line tool that runs and deploys scheduled jobs, hit the error "
    "in this report. pdt's own code is in {package}. The user's pdt project is the working "
    "folder. Write the part of a public GitHub issue that tells a pdt maintainer what failed. "
    "The user reads it before it is sent.\n"
    "1. Remove every secret and every private detail: passwords, tokens, keys, the names of "
    "people, companies, computers, apps, and projects, paths, host names, email addresses, "
    "and account, subscription, and project ids. Write <value> in place of each one.\n"
    "2. Give the error message, and add what you find in pdt's code and the project's files "
    "that helps a maintainer understand the failure, such as the pdt code that raised the "
    "error and the setting that led to it.\n"
    "3. Use at most 30 lines of Markdown, with no heading.\n"
    "Reply with only that text.")


@dataclass(frozen=True)
class Report:
    title: str
    body: str


@dataclass(frozen=True)
class Link:
    url: str
    whole: bool


def last_error(entries: list[dict], project: str, now: datetime) -> dict | None:
    for entry in reversed(entries):
        if entry.get("project") != project or str(entry.get("command", "")).startswith("pdt support"):
            continue
        if not watch.within(entry, now):
            return None
        if entry.get("outcome") in REPORTED:
            return entry
    return None


def providers() -> str:
    found = set()
    try:
        for name in config.find_apps():
            try:
                provider = config.merged_app(name)["platform"].get("provider")
            except config.ConfigError:
                continue
            found.add(provider if provider in config.PROVIDERS else "unknown")
    except (config.ConfigError, OSError, UnicodeError):
        pass
    return ", ".join(sorted(found)) or "none found"


def build(entry: dict) -> Report:
    """The report with only the structure of the error, which pdt can send with no check."""
    command, error = entry.get("command"), entry.get("error") or "no exception"
    what = "crashed" if entry.get("outcome") == "unhandled" else "failed"
    lines = [INTRO, "", "## What happened",
             f"Command: `{command}`",
             f"Result: {entry.get('outcome')}, exit code {entry.get('code')}",
             f"Exception: {error}"]
    if entry.get("where"):
        lines += ["", "## Where in pdt", *entry["where"]]
    lines += ["", "## Versions",
              f"pdt {entry.get('pdt')}, Python {entry.get('python')}, {entry.get('os')}, "
              f"providers of the apps: {providers()}"]
    return Report(f"pdt support: {command} {what}: {error}"[:120], "\n".join(lines))


def check_lines() -> list[str]:
    env = {**os.environ, watch.OFF_ENV: "0", "NO_COLOR": "1"}
    env.pop(watch.NOTE_ENV, None)
    try:
        with console.waiting("Running pdt validate"):
            done = subprocess.run([sys.executable, "-m", "pdt.cli", "validate", "--json"],
                                  env=env, stdin=subprocess.DEVNULL, capture_output=True,
                                  text=True, timeout=VALIDATE_SECONDS)
        found = json.loads(done.stdout)
    except subprocess.TimeoutExpired:
        return [f"pdt validate did not finish in {VALIDATE_SECONDS} seconds"]
    except (OSError, ValueError):
        return ["pdt validate stopped before it printed a result"]
    return [problem["message"] for problem in found["problems"]] or ["no problems"]


def drafted(entry: dict, report: Report, error: BaseException | None) -> Report | None:
    """The report with a part that a coding agent wrote from the full error, or None when no
    coding agent that the user allowed answered."""
    if not coding_agents.installed() or coding_agents.running_inside() is not None:
        return None
    try:
        if coding_agents.consent(coding_agents.read_settings()) is False:
            return None
    except (config.ConfigError, OSError, UnicodeError) as e:
        console.note(f"pdt did not ask a coding agent: {console.escape(str(e))}")
        return None
    console.say("pdt can ask a coding agent on this computer to write what failed, with the "
                "secrets and private details removed. Press Ctrl+C to skip it.")
    context = {"Report": report.body, "Error message": entry.get("message") or "none"}
    if error is not None:
        context["Traceback"] = "".join(traceback.format_exception(error)).strip()
    if entry.get("project"):
        context["pdt validate"] = "\n".join(check_lines())
    try:
        reply = coding_agents.ask(REQUEST.format(package=watch.PACKAGE), context, AGENT_SECONDS)
    except KeyboardInterrupt:
        console.say()
        console.note("pdt did not wait for the coding agent.")
        return None
    except (config.ConfigError, OSError, UnicodeError) as e:
        console.note(f"pdt did not ask a coding agent: {console.escape(str(e))}")
        return None
    if reply is None or reply.text == "":
        return None
    body = report.body.replace(INTRO, AGENT_INTRO.format(agent=reply.agent), 1)
    return Report(report.title, f"{body}\n\n## What failed\n{reply.text}")


def issue_link(title: str, body: str) -> Link:
    url = f"{ISSUES_URL}?{urlencode({'title': title, 'body': body})}"
    lines = body.splitlines()
    while len(url) > MAX_URL and lines:
        lines.pop()
        cut = "\n".join([*lines, "", CUT])
        url = f"{ISSUES_URL}?{urlencode({'title': title, 'body': cut})}"
    return Link(url, lines == body.splitlines())


def copy(text: str) -> bool:
    tools = [["pbcopy"], ["wl-copy"], ["xclip", "-selection", "clipboard"]]
    if os.name == "nt":
        tools.insert(0, ["clip"])
    for tool in tools:
        if shutil.which(tool[0]) is not None:
            try:
                done = subprocess.run(tool, input=text, text=True, check=False, timeout=5)
            except (OSError, subprocess.SubprocessError):
                return False
            return done.returncode == 0
    return False


def submit(report: Report) -> None:
    link = issue_link(report.title, report.body)
    copied = copy(report.body)
    if copied:
        console.say("pdt copied the full report to your clipboard.")
    if not link.whole:
        console.say("The link holds only the first part of the report. "
                    + ("Paste the rest from your clipboard." if copied
                       else "Copy the rest from the report above."))
    if not webbrowser.open(link.url):
        console.say("Open this link to start the issue:")
        console.command(link.url)
        return
    console.say("Your browser shows the new issue. Sign in to GitHub, check it, and select "
                f"{console.value('Create')} to send it.")


def show(report: Report) -> None:
    console.say("The issue is public. Anyone can read it. This is all it holds:")
    console.say()
    console.field("Title", report.title)
    for line in report.body.splitlines():
        console.bullet(console.escape(line))
    console.say()


def propose(entry: dict, error: BaseException | None = None) -> int:
    report = build(entry)
    console.say("pdt can start a GitHub issue about this problem, so the pdt team can fix it.")
    if not can_prompt(None):
        show(report)
        console.say(f"To report it, start an issue at {console.value(ISSUES_URL)} with this "
                    f"text, or ask on Discord: {console.value(watch.SUPPORT_URL)}")
        return 0
    report = drafted(entry, report, error) or report
    show(report)
    console.say("Your browser sends this text to GitHub. GitHub publishes it only when you "
                "check the issue and select Create.")
    if console.confirm("Does this report look right? Start it as a GitHub issue in your browser?"):
        submit(report)
    else:
        console.say(f"pdt sent nothing. You can also ask on Discord: {console.value(watch.SUPPORT_URL)}")
    return 0


def discord() -> int:
    console.say("pdt found no recent error to report.")
    console.say(f"Ask the pdt community on Discord: {console.value(watch.SUPPORT_URL)}")
    if can_prompt(None) and console.confirm("Open Discord in your browser?"):
        if not webbrowser.open(watch.SUPPORT_URL):
            console.command(watch.SUPPORT_URL)
    return 0


def main() -> int:
    entry = last_error(watch.recent(), watch.project(), datetime.now(timezone.utc))
    if entry is None:
        return discord()
    console.say(f"The last error pdt saw here: {console.value(entry.get('command'))} "
                f"at {console.value(entry.get('time'))}")
    return propose(entry)


def crashed(entry: dict, error: BaseException | None) -> None:
    if coding_agents.running_inside() is not None:
        return
    console.say()
    console.warn("pdt stopped because of a problem in pdt itself.")
    try:
        propose(entry, error)
    except (EOFError, KeyboardInterrupt):
        console.say()
    except Exception:
        console.say(f"pdt could not write a report. Ask on Discord: {console.value(watch.SUPPORT_URL)}")
