from __future__ import annotations

import json
import os
import platform
import re
import shutil
import subprocess
import sys
import traceback
import webbrowser
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
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
TRACE_FRAMES = 10
INTRO = ("`pdt support` wrote this report. It removed paths, web addresses, host names, email "
         "addresses, ids, app names, and the values in the user's .env files. `<arg>` stands for "
         "a word the user typed, and `<app>`, `<host>`, `<path>`, and `<value>` for what pdt "
         "removed.")
FILE_TYPES = {"py", "yml", "yaml", "ps1", "psd1", "psm1", "env", "json", "toml", "txt", "md",
              "lock", "exe", "sh", "bat", "cmd"}
HOST = re.compile(r"\b(?:[A-Za-z0-9_#-]+\.)+([A-Za-z][A-Za-z0-9_#-]*)\b")
CONFIG_SECTIONS = {"platform", "config", "env"}
QUOTED_PATH = re.compile(r"'[^'\n]*[/\\][^'\n]*'|\"[^\"\n]*[/\\][^\"\n]*\"")
RELATIVE_PATH = re.compile(r"(?<![\w<])[\w.-]+(?:[/\\][\w.-]+)+")
MISSING_MODULE = re.compile(r"No module named '?$")
CUT = "pdt cut the report here to fit the link. Paste the rest of it below."
REQUEST = (
    "A user of pdt, a command line tool that runs and deploys scheduled jobs, hit the problem "
    "in this report. pdt's own code is in {package}. The reader is an IT administrator, not a "
    "software engineer. Answer in plain words, in at most 12 short lines, with two parts:\n"
    "1. What a pdt developer still needs to know to fix this, that the report does not say. "
    "Write 'nothing' when the report is enough.\n"
    "2. The most likely way for the user to fix or work around the problem now.")


@dataclass(frozen=True)
class Report:
    title: str
    sections: dict[str, str]


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


def private_names() -> list[str]:
    names = [Path.home().name, platform.node(), platform.node().split(".")[0]]
    try:
        project = config.find_project()
        names += [project.name, *config.app_folders()]
        entries = config.load_yaml(project / config.PROJECT_FILE).get("apps")
        if isinstance(entries, list):
            names += [str(entry["name"]) for entry in entries
                      if isinstance(entry, dict) and "name" in entry]
    except (config.ConfigError, OSError, UnicodeError):
        pass
    return sorted({name for name in names if name != ""}, key=len, reverse=True)


def public_host(match: re.Match) -> str:
    token = match.group()
    one_file = token.count(".") == 1 and match.group(1).lower() in FILE_TYPES
    module = watch.PACKAGE.parent / token.replace(".", "/")
    pdt_module = token.startswith("pdt.") and (module.with_suffix(".py").is_file() or module.is_dir())
    config_key = token.split(".")[0] in CONFIG_SECTIONS
    powershell_module = all(label[:1].isupper() for label in token.split("."))
    if (one_file or pdt_module or config_key or powershell_module
            or MISSING_MODULE.search(match.string[:match.start()])):
        return token
    return "<host>"


def public(text: str, names: list[str]) -> str:
    text = QUOTED_PATH.sub("'<path>'", text)
    text = RELATIVE_PATH.sub(lambda match: match.group()
                             if (watch.PACKAGE / match.group()).is_file() else "<path>", text)
    text = HOST.sub(public_host, text)
    for name in names:
        for form in {name, re.sub(r"\d+", "#", name)}:
            text = re.sub(rf"(?<!\w){re.escape(form)}(?![\w#])", "<app>", text)
    return text


def install_method() -> str:
    prefix = Path(sys.prefix)
    clone = watch.PACKAGE.parent.parent
    if watch.PACKAGE.parent.name == "src" and (clone / "pyproject.toml").is_file():
        try:
            commit = subprocess.run(["git", "-C", str(clone), "rev-parse", "--short", "HEAD"],
                                    capture_output=True, text=True, timeout=10).stdout.strip()
            changed = subprocess.run(["git", "-C", str(clone), "status", "--porcelain"],
                                     capture_output=True, text=True, timeout=10).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            return "a clone of the repository"
        if commit == "":
            return "a clone of the repository"
        return f"a clone of the repository at {commit}" + (", with local changes" if changed else "")
    if prefix.parent.name == "tools" and prefix.parent.parent.name == "pdt":
        return "winget"
    if prefix.parent.name == "tools":
        return "uv tool install"
    return "another way, such as uvx"


def apps_lines(apps: list[str]) -> list[str]:
    counts = {}
    for name in apps:
        try:
            app = config.merged_app(name)
        except config.ConfigError:
            counts["apps with a config error"] = counts.get("apps with a config error", 0) + 1
            continue
        provider = app["platform"].get("provider")
        if provider not in config.PROVIDERS:
            provider = "an unknown provider"
        kind = "PowerShell" if config.powershell_scripts(app["dir"]) else "Python"
        label = f"{kind} apps on {provider}"
        counts[label] = counts.get(label, 0) + 1
    return [f"{label}: {count}" for label, count in counts.items()] or ["no apps"]


def check_lines(apps: list[str]) -> list[str]:
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
    problems = [watch.signature(problem["message"], apps) for problem in found["problems"]]
    return problems or ["no problems"]


def trace_lines(error: BaseException) -> list[str]:
    lines = []
    for frame in traceback.extract_tb(error.__traceback__):
        path = Path(frame.filename).resolve()
        if path.is_relative_to(watch.PACKAGE) and path.name != "watch.py":
            lines.append(f"{path.relative_to(watch.PACKAGE).as_posix()}:{frame.lineno} in {frame.name}")
    return lines[-TRACE_FRAMES:]


def build(entry: dict, error: BaseException | None = None) -> Report:
    names = private_names()
    signature = public(str(entry.get("signature", "")), names)
    sections = {
        "What happened": "\n".join([
            f"{entry.get('command')}: {entry.get('outcome')}, exit {entry.get('code')}, "
            f"at {entry.get('time')}",
            f"with pdt {entry.get('pdt')} on {entry.get('os')}, Python {entry.get('python')}",
            signature]),
        "This computer now": "\n".join([
            f"pdt {watch.version()}",
            f"{platform.system()} {platform.release()} ({platform.machine()})",
            f"Python {platform.python_version()}",
            f"installed with {install_method()}"]),
    }
    trace = trace_lines(error) if error is not None else []
    if trace:
        sections["Where in pdt"] = "\n".join(trace)
    if entry.get("project"):
        try:
            apps = config.find_apps()
            listing = apps_lines(apps)
        except Exception as e:
            apps = []
            listing = [f"pdt could not list them, because listing them also raised {type(e).__name__}"]
        sections["Apps in the project"] = "\n".join(listing)
        sections["pdt validate"] = "\n".join(public(line, names) for line in check_lines(apps))
    what = "crash" if entry.get("outcome") == "unhandled" else f"{entry.get('command')} failed"
    return Report(f"pdt support: {what}: {signature}"[:120], sections)


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


def submit(title: str, body: str) -> None:
    link = issue_link(title, body)
    copied = copy(body)
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


def advise(report: Report) -> None:
    if not coding_agents.installed() or coding_agents.running_inside() is not None:
        return
    console.say("pdt can ask a coding agent on this computer to check the report and suggest "
                "a fix. Press Ctrl+C to skip it.")
    try:
        reply = coding_agents.ask(REQUEST.format(package=watch.PACKAGE), report.sections,
                                  AGENT_SECONDS)
    except KeyboardInterrupt:
        console.say()
        console.note("pdt did not wait for the coding agent.")
        return
    except (config.ConfigError, OSError, UnicodeError) as e:
        console.note(f"pdt did not ask a coding agent: {console.escape(str(e))}")
        return
    if reply is None or reply.text == "":
        return
    console.heading(f"Advice from {console.escape(reply.agent)} (a coding agent, not the pdt team):")
    for line in reply.text.splitlines():
        console.bullet(console.escape(line))


def propose(entry: dict, error: BaseException | None = None) -> int:
    report = build(entry, error)
    secrets = coding_agents.env_secrets().values
    title = coding_agents.message(report.title, {}, secrets)
    body = coding_agents.message(INTRO, report.sections, secrets)
    console.say("pdt can start a GitHub issue about this problem, so the pdt team can fix it.")
    console.say("The issue is public. Anyone can read it. This is all it holds:")
    console.say()
    console.field("Title", title)
    for line in body.splitlines():
        console.bullet(console.escape(line))
    console.say()
    advise(report)
    if not can_prompt(None):
        console.say("Open this link to start the issue. Nothing is sent until you select Create:")
        console.command(issue_link(title, body).url)
        return 0
    console.say("Nothing is sent until you check the issue in your browser and select Create.")
    if console.confirm("Start this GitHub issue in your browser?"):
        submit(title, body)
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
