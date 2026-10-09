"""Every value in a console message goes through console.value() or console.escape().

A console message is rich markup. A value interpolated as is prints unstyled,
and a `[` in it can swallow text or break the line. This test reads src/pdt
with ast and fails on each such value, so a new message keeps the rule.
"""

import ast
from pathlib import Path

from rich.errors import MarkupError
from rich.text import Text

SRC = Path(__file__).resolve().parent.parent / "src" / "pdt"

# The console functions that read markup, and deploy_common.fail, which prints its
# message with console.error.
MARKUP = {"say", "status", "error", "note", "warn", "step", "done", "failed", "heading",
          "detail", "bullet", "styled", "Markup"}
WRAPPERS = {"value", "escape"}
# Functions that return markup built with console.value().
BUILDERS = {"project_line", "secret_contents", "store_description", "store_kept_line",
            "kept_line", "_kept_storage_line", "users_note"}

# Text that pdt wrote and that is not a value, so it prints unstyled: a count, a
# time, a state word, a fixed phrase, or a variable that holds markup built with
# console.value(). Each set holds the interpolated expressions of one file.
ALLOWED = {
    "cli.py": {"APP_QUESTIONS[command]", "len(apps) - len(shown)", "len(problems)", "index",
               "len(names)", "line", "choices"},
    "deploy.py": {"tool", "line", "state", "action"},
    "deploy_common.py": {"what", "message", "description", "count", "provider", "len(values)",
                         "details['tries']", "BACKOFF_MAX_TRIES", "details['wait']"},
    "deploy_aws.py": {"delay"},
    "deploy_aws_batch.py": {"what", "resource.label", "label", "DOCKER_PLATFORM",
                            "float(JOB_VCPU)", "JOB_MEMORY", "groups", "len(arns)",
                            "len(revisions)", "state"},
    "deploy_azure.py": {"reason", "wait", "label", "state", "waited"},
    "deploy_azure_container_apps.py": {"timestamp", "shared", "self.label", "lock.label",
                                       "release.note"},
    "deploy_google_cloud.py": {"wait", "label", "secret_state", "resource"},
    "deploy_windows.py": {"verb", "names"},
    "duckdb_wasm.py": {"size_text(size)"},
    "email_auth.py": {"_provider_name(provider)", "name"},
    "gcloud_sdk.py": {"VERSION"},
    "pwsh.py": {"VERSION"},
    "regions.py": {"PROVIDER_NAMES[provider]"},
    "runs_cli.py": {"local_text(since)", "local_text(since + span)", "len(shown)", "len(found)",
                    "side", "len(lines)", "total", "store", "local_text(run.ended, '%H:%M:%S')",
                    "minutes_text(delay)"},
    "scaffold.py": {"question", "len(labels)"},
    "storage_cli.py": {"len(objects)"},
}


def called(node: ast.AST) -> str:
    """The name of the function `node` calls: `step` for console.step(...)."""
    if isinstance(node, ast.Call):
        if isinstance(node.func, ast.Attribute):
            return node.func.attr
        if isinstance(node.func, ast.Name):
            return node.func.id
    return ""


def reads_as_markup(text: str) -> bool:
    try:
        return Text.from_markup(text, emoji=False).plain != text
    except MarkupError:
        return True


def problems(node: ast.AST, file: str) -> list[str]:
    if isinstance(node, ast.Constant):
        if isinstance(node.value, str) and reads_as_markup(node.value):
            return [f"{node.value!r} reads as markup; escape it"]
        return []
    if isinstance(node, ast.JoinedStr):
        return [problem for part in node.values for problem in problems(part, file)]
    if isinstance(node, ast.FormattedValue):
        return problems(node.value, file)
    if isinstance(node, ast.IfExp):
        return problems(node.body, file) + problems(node.orelse, file)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return problems(node.left, file) + problems(node.right, file)
    if called(node) in WRAPPERS | BUILDERS:
        return []
    if called(node) == "join" and isinstance(node.args[0], (ast.GeneratorExp, ast.ListComp)):
        return problems(node.args[0].elt, file)
    if ast.unparse(node) in ALLOWED.get(file, set()):
        return []
    return [f"{ast.unparse(node)} is not wrapped in console.value() or console.escape()"]


def unwrapped() -> list[str]:
    found = []
    for path in sorted(SRC.rglob("*.py")):
        if path.name == "console.py":
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.Call):
                continue
            if not (isinstance(node.func, ast.Name) and node.func.id == "fail"
                    or isinstance(node.func, ast.Attribute) and node.func.attr in MARKUP
                    and isinstance(node.func.value, ast.Name) and node.func.value.id == "console"):
                continue
            for arg in node.args:
                found.extend(f"{path.name}:{node.lineno}: {problem}"
                             for problem in problems(arg, path.name))
    return found


def test_every_value_in_a_console_message_is_wrapped():
    assert unwrapped() == []


def test_the_check_finds_a_bare_value_and_a_literal_bracket():
    tree = ast.parse('f"Created {name} [y/N]"', mode="eval")
    assert problems(tree.body, "x.py") == [
        "name is not wrapped in console.value() or console.escape()",
        "' [y/N]' reads as markup; escape it",
    ]
    tree = ast.parse('f"Created {console.value(name)}"', mode="eval")
    assert problems(tree.body, "x.py") == []
