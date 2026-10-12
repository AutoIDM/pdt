"""Every `pdt <command>` that the docs or pdt's own hints name exists in the parser.

The mentions come from the Markdown docs, the example apps, the GUI templates,
and the string literals in src/pdt other than docstrings: each `pdt ...` in
backticks, each string given to a call that shows a command (`console.command`
and the like), and each "run pdt ..." or "with: pdt ..." in a message. The check
is the subcommand, its --options, and the action of `pdt storage` and `pdt secrets`.
"""

import ast
import re
from pathlib import Path

import pytest

from pdt import cli, deploy_common, storage_cli

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src" / "pdt"
DOCS = [ROOT / "README.md", ROOT / "AGENTS.md", ROOT / "verify" / "README.md",
        *sorted((ROOT / "docs").rglob("*.md"))]
# Backticked text that starts with "pdt " but is not a command: README's
# mistyped command, and the display name of the Google Cloud service account.
NOT_COMMANDS = {"pdt lgos", "pdt job runner"}
COMMAND_CALLS = {"command", "field", "next_steps", "deployed_next_steps"}
SPAN = re.compile(r"`(pdt [^`]+)`")
SAID = re.compile(r"(?i:\brun|\bwith:?|\bexample:?)\s+(pdt [^`;,)\n]+)")
TEMPLATE = re.compile(r'data-copy="(pdt [^"]+)"|data-copy>(pdt [^<]+)<|<code>(pdt [^<]+)</code>')
MARKUP = re.compile(r"\[/?[a-z ]*\]")
ACTIONS = {"storage": storage_cli.COMMANDS, "secrets": deploy_common.SECRET_ACTIONS}


def markdown_mentions(text: str) -> list[str]:
    found = SPAN.findall(text)
    fenced = False
    for line in text.splitlines():
        if line.startswith("```"):
            fenced = not fenced
        elif fenced and line.startswith("pdt "):
            found.append(line)
    return found


def text_of(node) -> str | None:
    """A string literal's text, with "{}" for each value an f-string fills in."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(part.value if isinstance(part, ast.Constant) else "{}"
                       for part in node.values)
    return None


def python_mentions(source: str) -> list[str]:
    tree = ast.parse(source)
    skip = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.body and text_of(getattr(node.body[0], "value", None)) is not None:
                skip.add(id(node.body[0].value))
        if isinstance(node, ast.JoinedStr):
            skip.update(id(part) for part in node.values)
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(
                node.func, "attr", getattr(node.func, "id", None)) in COMMAND_CALLS:
            for arg in node.args:
                for inner in ast.walk(arg):
                    text = text_of(inner)
                    if text is not None and id(inner) not in skip and text.startswith("pdt "):
                        found.append(MARKUP.sub("", text))
        text = text_of(node)
        if text is None or id(node) in skip:
            continue
        text = MARKUP.sub("", text)
        found += SPAN.findall(text) + SAID.findall(text)
    return found


def template_mentions(text: str) -> list[str]:
    text = re.sub(r"\{\{.*?\}\}", "{}", text)
    return ["".join(groups) for groups in TEMPLATE.findall(text)]


def mentions() -> list[tuple[str, str]]:
    sources = [(path, markdown_mentions) for path in DOCS]
    sources += [(path, python_mentions) for path in sorted(SRC.rglob("*.py"))]
    sources += [(path, template_mentions) for path in sorted(SRC.rglob("*.html"))]
    sources += [(path, SPAN.findall) for path in sorted((SRC / "examples").rglob("*"))
                if path.is_file() and path.suffix != ".pyc"]
    found = set()
    for path, extract in sources:
        where = path.relative_to(ROOT).as_posix()
        found.update((where, command) for command in extract(path.read_text(encoding="utf-8")))
    return sorted(found)


PARSER = cli.build_parser()


def problem(command: str) -> str:
    """Why `command` would not run as written, or "" when its command and options exist."""
    words = command.replace("\\|", "|").split()
    if command in NOT_COMMANDS or "{}" in words[1:2]:
        return ""
    names = words[1].split("|") if len(words) > 1 else [""]
    if len(names) > 1:
        return next((found for found in (problem(f"pdt {name}") for name in names) if found), "")
    name = names[0]
    if name.startswith("-"):
        return "" if name in PARSER._option_string_actions else f"pdt has no option {name}"
    sub = PARSER.commands.get(name)
    if sub is None:
        return f"pdt has no command {name!r}"
    if name in cli.CLOUD_CLIS:
        return ""
    rest = [word.strip("[](),.") for word in words[2:]]
    allowed = set(sub._option_string_actions)
    if name == "storage":
        allowed |= set(re.findall(r"--[a-z-]+", storage_cli.USAGE))
    for word in rest:
        if word.startswith("--") and word.split("=")[0] not in allowed:
            return f"pdt {name} has no option {word.split('=')[0]}"
    values = [word for word in rest if not word.startswith("-")]
    if name in ACTIONS and len(values) > 1 and not any(
            all(part in ACTIONS[name] or "{}" in part for part in word.split("|"))
            for word in values[:2]):
        return f"pdt {name} has no action in {' '.join(values[:2])!r}"
    return ""


MENTIONS = mentions()


@pytest.mark.parametrize("where,command", MENTIONS, ids=[f"{w}: {c}" for w, c in MENTIONS])
def test_every_command_the_docs_and_hints_name_exists(where, command):
    assert problem(command) == "", f"{where}: `{command}`"


def test_mentions_come_from_every_kind_of_source():
    sources = {where for where, _command in MENTIONS}
    assert {"README.md", "AGENTS.md", "src/pdt/scaffold.py", "src/pdt/cli.py",
            "src/pdt/utils/storage.py", "src/pdt/gui/templates/gui/run.html",
            "src/pdt/examples/hello-world/config.yml"} <= sources
    commands = {command for _where, command in MENTIONS}
    assert "pdt storage APP ls\\|get\\|query\\|unlock\\|destroy" in commands
    assert "pdt storage {} unlock" in commands
    assert "pdt list" in commands


@pytest.mark.parametrize("command,expected", [
    ("pdt start my-report", "pdt has no command 'start'"),
    ("pdt logs my-report --run-id 3", "pdt logs has no option --run-id"),
    ("pdt storage my-report rm runs/", "pdt storage has no action in 'my-report rm'"),
    ("pdt secrets my-report push", "pdt secrets has no action in 'my-report push'"),
    ("pdt storage my-report ls --json", ""),
    ("pdt logs APP [N] [--count 5]", ""),
    ("pdt aws batch submit-job --job-name x", ""),
    ("pdt --version", ""),
    ("pdt --verbose", "pdt has no option --verbose"),
    ("pdt validate|run|deploy", ""),
    ("pdt validate|start", "pdt has no command 'start'"),
])
def test_problem_names_what_does_not_exist(command, expected):
    assert problem(command) == expected
