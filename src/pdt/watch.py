"""Point a person at the pdt community after a pdt command fails at a terminal.

It sends nothing anywhere.
"""

from __future__ import annotations

import os
import sys
import traceback
from typing import Callable

from pdt import config

SUPPORT_URL = "https://discord.gg/4j83nqnNfK"
OFF_ENV = "PDT_ERROR_WATCH"
SETTING = "error-watch"
AGENT_ENVS = ("CLAUDECODE", "CURSOR_AGENT", "CODEX_SANDBOX", "GEMINI_CLI")


def is_flag(token: str) -> bool:
    long_flag = token.startswith("--")
    short_flag = token.startswith("-") and len(token) == len("-x")
    return long_flag or short_flag


def command_type(argv: list[str], commands: set[str]) -> str:
    word = next((token for token in argv[1:] if not is_flag(token)), "")
    return f"pdt {word}" if word in commands else "pdt"


def exit_code(code: object) -> int:
    if code is None:
        return 0
    return code if isinstance(code, int) else 1


def off() -> bool:
    if os.environ.get(OFF_ENV, "").strip().lower() in ("0", "false", "off", "no"):
        return True
    try:
        return config.load_yaml(config.find_project() / config.PROJECT_FILE).get(SETTING) is False
    except (config.ConfigError, OSError, UnicodeDecodeError):
        return False


def can_offer() -> bool:
    from pdt.utils.email_auth import can_prompt
    return (can_prompt(None) and sys.stderr.isatty()
            and not any(len(token) > 2 and "--json".startswith(token) for token in sys.argv)
            and not any(name in os.environ for name in AGENT_ENVS))


def watched(main: Callable[[], int], commands: set[str] = frozenset()) -> int:
    from pdt import completion
    if completion.COMPLETING or os.environ.get("CI", "").strip() != "" or off():
        return main()
    try:
        result = main()
    except SystemExit as e:
        offer(exit_code(e.code), commands)
        raise
    except Exception:
        traceback.print_exc()
        offer(1, commands)
        return 1
    offer(exit_code(result), commands)
    return result


def offer(code: int, commands: set[str]) -> None:
    from pdt import console
    from pdt.deploy import DECLINED
    if code in (0, DECLINED, 130) or not can_offer():
        return
    console.say(f"Get help with {console.escape(command_type(sys.argv, commands))} "
                f"at {console.value(SUPPORT_URL)}")
