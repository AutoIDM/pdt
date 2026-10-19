"""Ask a coding agent on this computer (Claude Code, Codex, or Cursor) for help.

`ask` is the one entry point. It sends a request and the context the caller
chooses to one agent, at low effort and with read-only access, and returns
the reply. The user consents once per agent, and pdt asks which agent to use
only when more than one could answer. Both answers live in the user's
settings file (`SETTINGS`), not in the project. Inside a coding agent's own
session, pdt starts no second agent: it prints the request for the agent
that is already running.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from pdt import config, console
from pdt.config import ConfigError
from pdt.utils.email_auth import can_prompt

SETTINGS = config.data_home() / "pdt" / "settings.yml"
CHOICE_KEY = "coding-agent"
CONSENT_KEY = "coding-agent-consent"
REMOVED = "<secret removed>"
# A value this short, such as 1 or yes, is not a secret, and removing it would change many words.
SHORTEST_SECRET = 4
REPLY_SECONDS = 300
# Windows env var names ignore case, so API_TOKEN in the environment is api_token in a .env file.
ENV_NAME = str.upper if os.name == "nt" else str
LIST_SECONDS = 60


class AgentCommandError(Exception):
    pass


def _claude_args(program: str, env: dict[str, str]) -> list[str]:
    # --tools limits only the built-in tools, so MCP servers and hooks are turned off too.
    return [program, "-p", "--effort", "low", "--tools", "Read,Grep,Glob", "--strict-mcp-config",
            "--settings", '{"disableAllHooks": true}', "--no-session-persistence"]


def _codex_args(program: str, env: dict[str, str]) -> list[str]:
    # The user's config.toml can add MCP servers and hooks that the sandbox does not cover.
    return [program, "exec", "--skip-git-repo-check", "--ephemeral", "--ignore-user-config",
            "--color", "never", "-s", "read-only", "-c", 'model_reasoning_effort="low"', "-"]


def _cursor_args(program: str, env: dict[str, str]) -> list[str]:
    # Cursor has no effort option: the effort is part of the model id, and the ids differ
    # from one account to the next.
    try:
        listing = subprocess.run([program, "models"], stdin=subprocess.DEVNULL, env=env,
                                 capture_output=True, text=True, encoding="utf-8",
                                 errors="replace", timeout=LIST_SECONDS)
    except OSError as e:
        raise AgentCommandError(f"pdt could not start {program} models: {e}")
    except subprocess.TimeoutExpired:
        raise AgentCommandError(f"{program} models did not answer in {LIST_SECONDS} seconds")
    if listing.returncode != 0:
        raise AgentCommandError(f"{program} models stopped with exit code {listing.returncode}: "
                                f"{listing.stderr.strip()}")
    models = [line.split(" - ", 1)[0].strip() for line in listing.stdout.splitlines()
              if " - " in line]
    low = next((model for model in models if model.endswith("-low")), None)
    if low is None:
        raise AgentCommandError(f"{program} models lists no low-effort model for your account")
    return [program, "-p", "--trust", "--mode", "ask", "--model", low]


@dataclass(frozen=True)
class Agent:
    name: str
    label: str
    program: str
    session_var: str
    args: Callable[[str, dict[str, str]], list[str]]


AGENTS = [
    Agent("claude", "Claude Code", "claude", "CLAUDECODE", _claude_args),
    Agent("codex", "Codex", "codex", "CODEX_THREAD_ID", _codex_args),
    Agent("cursor", "Cursor", "cursor-agent", "CURSOR_AGENT", _cursor_args),
]


def running_inside() -> Agent | None:
    return next((agent for agent in AGENTS
                 if os.environ.get(agent.session_var, "").strip() != ""), None)


def installed() -> dict[str, str]:
    found = {agent.name: shutil.which(agent.program) for agent in AGENTS}
    return {name: path for name, path in found.items() if path}


def read_settings() -> dict:
    settings = config.load_yaml(SETTINGS)
    answers = settings.get(CONSENT_KEY) or {}
    if not isinstance(answers, dict) or not all(isinstance(answer, bool)
                                                for answer in answers.values()):
        raise ConfigError(f"{SETTINGS}: {CONSENT_KEY} must list each coding agent with true "
                          "or false, for example `claude: false` on its own line under it")
    if not isinstance(settings.get(CHOICE_KEY, ""), str):
        raise ConfigError(f"{SETTINGS}: {CHOICE_KEY} must be one of "
                          f"{', '.join(agent.name for agent in AGENTS)}")
    return settings


def remember(key: str, value, merge: bool = False) -> None:
    import yaml
    SETTINGS.parent.mkdir(parents=True, exist_ok=True)
    with config.locked(SETTINGS):
        settings = read_settings()
        if merge:
            value = {**(settings.get(key) or {}), **value}
        settings[key] = value
        config.write_text_atomically(SETTINGS, yaml.safe_dump(settings, sort_keys=False))


def consent(settings: dict, agent: Agent) -> bool | None:
    return (settings.get(CONSENT_KEY) or {}).get(agent.name)


@dataclass(frozen=True)
class EnvSecrets:
    names: set[str]
    values: set[str]


def env_secrets() -> EnvSecrets:
    from dotenv import dotenv_values
    files = config.find_env_files(Path.cwd())
    try:
        project = config.find_project()
        files += [project / ".env", *sorted(project.glob("*/.env"))]
    except ConfigError:
        pass
    files = [path for path in dict.fromkeys(path.resolve() for path in files) if path.is_file()]
    names, values = set(), set()
    for path in files:
        for name, value in dotenv_values(path).items():
            names.add(name)
            values.update({value or "", os.environ.get(name, "")})
    return EnvSecrets(names, {value for value in values if len(value) >= SHORTEST_SECRET})


def message(request: str, context: dict[str, str], secrets: set[str]) -> str:
    text = "\n\n".join([request, *(f"## {heading}\n{body}" for heading, body in context.items())])
    for value in sorted(secrets, key=len, reverse=True):
        text = text.replace(value, REMOVED)
    return text


def choose(settings: dict, found: dict[str, str], ask_user: bool) -> Agent | None:
    usable = [agent for agent in AGENTS if agent.name in found
              and consent(settings, agent) is not False and (ask_user or consent(settings, agent))]
    stored = next((agent for agent in usable if agent.name == settings.get(CHOICE_KEY)), None)
    if stored is not None or len(usable) == 1:
        return stored or usable[0]
    if not usable and ask_user:
        console.note("pdt did not ask a coding agent for help, because you said no to each one "
                     f"on this computer. Edit {console.value(str(SETTINGS))} to change it.")
        return None
    if not usable:
        console.note("pdt did not ask a coding agent for help, because you have not allowed "
                     "one yet. The question shows only in a terminal window.")
        return None
    if not ask_user:
        console.note(f"more than one coding agent is on this computer, and pdt cannot ask "
                     f"which one to use here. Set {console.value(CHOICE_KEY)} in "
                     f"{console.value(str(SETTINGS))}.")
        return None
    console.say("More than one coding agent is on this computer.")
    for number, agent in enumerate(usable, 1):
        console.choice(number, agent.label)
    while True:
        try:
            answer = console.ask("Which one should pdt use?", "1") or "1"
        except EOFError:
            return None
        if answer.isdigit() and 1 <= int(answer) <= len(usable):
            break
        console.warn(f"Type a number from 1 to {len(usable)}.")
    agent = usable[int(answer) - 1]
    remember(CHOICE_KEY, agent.name)
    return agent


def allowed(settings: dict, agent: Agent, context: dict[str, str]) -> bool:
    answer = consent(settings, agent)
    if answer is not None:
        return answer
    label = console.value(agent.label)
    sent = f" and this text: {console.escape(', '.join(context))}" if context else ""
    console.say(f"pdt can ask {label}, a coding agent on this computer, for help.")
    console.say(f"pdt sends it a request{sent}. "
                f"pdt first removes each value of {SHORTEST_SECRET} or more characters "
                "in your .env files.")
    console.say(f"{label} can read files on this computer. It cannot change them.")
    console.say(f"pdt saves your answer in {console.value(str(SETTINGS))}. "
                "Edit that file to change it.")
    try:
        answer = console.confirm(f"Let pdt send requests to {agent.label}?")
    except EOFError:
        return False
    remember(CONSENT_KEY, {agent.name: answer}, merge=True)
    return answer


def ask(request: str, context: dict[str, str] | None = None) -> str | None:
    """Send `request` and each `context` section (a heading and its text) to a coding agent
    at low effort, and return its reply. Return None when no agent answered; pdt has then
    said why, or, inside a coding agent's session, printed the request for that agent."""
    context = context or {}
    secrets = env_secrets()
    inside = running_inside()
    if inside is not None:
        console.note(f"pdt runs inside {console.value(inside.label)}, so it does not start "
                     "another coding agent. This is the request for it:")
        console.say(console.escape(message(request, context, secrets.values)))
        return None
    found = installed()
    if not found:
        console.note("pdt found no coding agent on this computer: "
                     f"{', '.join(console.value(agent.program) for agent in AGENTS)}.")
        return None
    settings = read_settings()
    ask_user = can_prompt(None)
    agent = choose(settings, found, ask_user)
    if agent is None or not allowed(settings, agent, context):
        return None
    label = console.value(agent.label)
    hidden = {ENV_NAME(name) for name in secrets.names}
    env = {name: value for name, value in os.environ.items() if ENV_NAME(name) not in hidden}
    try:
        command = agent.args(found[agent.name], env)
    except AgentCommandError as e:
        console.note(f"pdt did not ask {label} for help: {console.escape(str(e))}.")
        return None
    try:
        with console.waiting(f"Waiting for {agent.label}"):
            result = subprocess.run(command, input=message(request, context, secrets.values),
                                    env=env, capture_output=True, text=True, encoding="utf-8",
                                    errors="replace", timeout=REPLY_SECONDS)
    except OSError as e:
        console.error(f"pdt could not start {label}: {console.escape(str(e))}")
        return None
    except subprocess.TimeoutExpired:
        console.error(f"{label} did not answer in {console.value(REPLY_SECONDS)} seconds.")
        return None
    if result.returncode != 0:
        console.error(f"{console.value(agent.label)} stopped with exit code "
                      f"{console.value(result.returncode)}.")
        for line in result.stderr.strip().splitlines()[-5:]:
            console.detail(console.escape(line))
        return None
    return result.stdout.strip()


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="python -m pdt.coding_agents")
    parser.add_argument("request", nargs="?", help="what to ask the coding agent")
    parser.add_argument("--context", action="append", default=[], metavar="HEADING=TEXT",
                        help="a section of text to send with the request; repeat it for more")
    args = parser.parse_args(argv)
    try:
        settings = read_settings()
    except ConfigError as e:
        console.error(console.escape(str(e)))
        return 1
    found, inside = installed(), running_inside()
    states = {True: "allowed", False: "not allowed", None: "not asked yet"}
    for agent in AGENTS:
        where = found.get(agent.name, "not found")
        console.field(agent.label, f"{where}, {states[consent(settings, agent)]}")
    console.field("Settings file", str(SETTINGS))
    if settings.get(CHOICE_KEY):
        console.field("Agent to use", str(settings[CHOICE_KEY]))
    if inside is not None:
        console.field("This process runs inside", inside.label)
    if args.request is None:
        return 0
    context = {}
    for item in args.context:
        heading, _, text = item.partition("=")
        context[heading] = text
    reply = ask(args.request, context)
    if reply is None:
        return 1
    console.say(console.escape(reply))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
