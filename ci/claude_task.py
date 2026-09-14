"""Run one Claude Code task from GitLab CI on the team's Claude subscription.

A task is a folder under ci/claude-tasks/<name>/:

  task.yml    model, budget_usd, timeout_minutes, permission_mode,
              allowed_tools, disallowed_tools, and the optional hook
              filenames ``select`` and ``check``.
  prompt.md   the prompt Claude gets. ``{field}`` placeholders are filled
              from the item the select hook produced.
  system.md   optional text appended to Claude's system prompt.
  select      optional before-hook. Prints a JSON list of item dicts; each
              item becomes one Claude session. Absent means one session
              with an empty item. An empty list means nothing to do.
  check       optional after-hook. Gets ``{"item", "result", "job_url"}`` on
              stdin and prints ``{"status": "ok"|"needs_human"|"error",
              "message": "..."}``. It may have side effects, for example an
              MR comment. Absent means ``ok`` unless Claude reported an error.

Usage: ci/claude_task.py <task-name> [--dry-run] [--only ID]

Exit codes: 0 every item ok, 2 an item needs a person (the CI job shows a
warning), 1 the token is bad, the task is misconfigured, or an item errored.

Environment:
  CLAUDE_TOKEN   the Claude Code OAuth token from ``claude setup-token``. The
                 CI job exports it as CLAUDE_CODE_OAUTH_TOKEN, which is the
                 name Claude Code reads. Both names are accepted here.
  CI_JOB_URL     handed to the check hook so an MR comment can link the log.

The token is verified before anything else runs: the variable must be set,
``claude auth status`` must report a logged-in subscription, and a tiny probe
prompt must succeed, because a revoked or expired token still looks logged in
locally. How to create the token is in the handbook (developer.md, "Claude
Code in CI").

Stdlib plus pyyaml, which is already a core dependency of pdt.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

import yaml

TASKS_DIR = Path(__file__).resolve().parent / "claude-tasks"
REPORT_DIR = Path("claude-task-report")
HANDBOOK = "see the handbook: developer.md > Gitlab > CI > Claude Code in CI"
PROBE_PROMPT = "Reply with the single word OK and nothing else."
PROBE_BUDGET_USD = 0.10
PROBE_TIMEOUT_SECONDS = 120
HOOK_TIMEOUT_SECONDS = 600
STATUSES = ("ok", "needs_human", "error")
EXIT_OK, EXIT_ERROR, EXIT_NEEDS_HUMAN = 0, 1, 2
TOKEN_NAMES = ("CLAUDE_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN")
# "oauth_token" is what `claude auth status` reports for a token taken from the
# environment (the CI case); "claude.ai" is an interactive login on a laptop.
SUBSCRIPTION_AUTH_METHODS = ("oauth_token", "claude.ai")
KNOWN_KEYS = {"model", "budget_usd", "timeout_minutes", "permission_mode",
              "allowed_tools", "disallowed_tools", "select", "check"}


class TaskError(Exception):
    """A message for the job log. It names the file or variable to fix."""


@dataclass
class Task:
    name: str
    folder: Path
    prompt: str
    model: str = "opus"
    budget_usd: float = 10.0
    timeout_minutes: int = 30
    permission_mode: str = "acceptEdits"
    allowed_tools: list[str] = field(default_factory=list)
    disallowed_tools: list[str] = field(default_factory=list)
    select: Path | None = None
    check: Path | None = None
    system: Path | None = None


# --- task folder --------------------------------------------------------------

def load_task(name: str, tasks_dir: Path = TASKS_DIR) -> Task:
    folder = tasks_dir / name
    config = folder / "task.yml"
    prompt = folder / "prompt.md"
    if not config.is_file():
        raise TaskError(f"no task named {name!r}: {config} does not exist")
    if not prompt.is_file():
        raise TaskError(f"{prompt} is missing; every task needs a prompt.md")
    raw = yaml.safe_load(config.read_text()) or {}
    if not isinstance(raw, dict):
        raise TaskError(f"{config} must hold a mapping of settings")
    unknown = sorted(set(raw) - KNOWN_KEYS)
    if unknown:
        raise TaskError(f"{config}: unknown key(s) {', '.join(unknown)}")
    task = Task(name=name, folder=folder, prompt=prompt.read_text())
    if "model" in raw:
        task.model = _text(config, "model", raw["model"])
    if "permission_mode" in raw:
        task.permission_mode = _text(config, "permission_mode", raw["permission_mode"])
    if "budget_usd" in raw:
        task.budget_usd = _number(config, "budget_usd", raw["budget_usd"])
    if "timeout_minutes" in raw:
        task.timeout_minutes = int(_number(config, "timeout_minutes", raw["timeout_minutes"]))
    if "allowed_tools" in raw:
        task.allowed_tools = _strings(config, "allowed_tools", raw["allowed_tools"])
    if "disallowed_tools" in raw:
        task.disallowed_tools = _strings(config, "disallowed_tools", raw["disallowed_tools"])
    if raw.get("select"):
        task.select = _hook(config, folder, "select", raw["select"])
    if raw.get("check"):
        task.check = _hook(config, folder, "check", raw["check"])
    system = folder / "system.md"
    task.system = system if system.is_file() else None
    return task


def _text(config: Path, key: str, value) -> str:
    if not isinstance(value, str) or not value.strip():
        raise TaskError(f"{config}: {key} must be a non-empty string")
    return value.strip()


def _number(config: Path, key: str, value) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise TaskError(f"{config}: {key} must be a number above zero")
    return float(value)


def _strings(config: Path, key: str, value) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(v, str) and v.strip() for v in value):
        raise TaskError(f"{config}: {key} must be a list of strings")
    return [v.strip() for v in value]


def _hook(config: Path, folder: Path, key: str, value) -> Path:
    hook = folder / _text(config, key, value)
    if not hook.is_file():
        raise TaskError(f"{config}: {key} names {hook.name}, which is not in {folder}")
    if hook.suffix == ".py" and hook.stem in sys.stdlib_module_names:
        # Python puts the script's folder first on sys.path, so a hook called
        # select.py hides the standard library's select module and crashes.
        raise TaskError(f"{config}: {key} is named {hook.name}, which shadows Python's "
                        f"{hook.stem} module; rename it (for example {hook.stem}_mrs.py)")
    return hook


def render_prompt(template: str, item: dict) -> str:
    try:
        return template.format_map(item)
    except KeyError as missing:
        raise TaskError(
            f"prompt.md uses {{{missing.args[0]}}} but the select hook gave "
            f"no {missing.args[0]!r} field") from None
    except (ValueError, IndexError) as problem:
        raise TaskError(f"prompt.md has a bad placeholder: {problem}") from None


def item_id(item: dict, index: int) -> str:
    raw = item.get("id", index)
    return "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in str(raw))


# --- token verification (pure parts) -----------------------------------------

def token_from(env) -> str:
    for name in TOKEN_NAMES:
        if env.get(name, "").strip():
            return env[name].strip()
    return ""


def missing_token_problem(env) -> str:
    if token_from(env):
        return ""
    return ("CLAUDE_TOKEN is not set. Add it as a masked, protected CI variable "
            f"holding the output of `claude setup-token`; {HANDBOOK}.")


def auth_status_problem(output: str) -> str:
    """Judge `claude auth status --json`. Empty string means it looks fine."""
    try:
        status = json.loads(output)
    except (json.JSONDecodeError, TypeError):
        return ("CLAUDE_TOKEN is not a valid Claude Code OAuth token: "
                "`claude auth status` gave no JSON.")
    if not isinstance(status, dict) or not status.get("loggedIn"):
        return ("CLAUDE_TOKEN is not a valid Claude Code OAuth token: Claude Code "
                f"is not logged in. Run `claude setup-token` again; {HANDBOOK}.")
    if status.get("authMethod") not in SUBSCRIPTION_AUTH_METHODS:
        return ("CLAUDE_TOKEN is not a subscription token (auth method is "
                f"{status.get('authMethod')!r}). Run `claude setup-token`; {HANDBOOK}.")
    return ""


def auth_identity(output: str) -> str:
    """Who is paying, as far as `claude auth status` says.

    A token from the environment reports only the auth method; an
    interactive login also reports the email and plan.
    """
    try:
        status = json.loads(output)
    except (json.JSONDecodeError, TypeError):
        return "unknown"
    email = status.get("email")
    plan = status.get("subscriptionType")
    if not email and not plan:
        return f"a subscription token (auth method {status.get('authMethod')})"
    return f"{email or 'unknown'} ({plan or 'unknown'} subscription)"


def parse_claude_result(returncode: int, output: str) -> dict:
    """Turn `claude -p --output-format json` output into a result dict.

    Anything that is not a JSON object becomes an error result, so callers
    never special-case a crash, a timeout, or an empty reply.
    """
    try:
        result = json.loads(output)
    except (json.JSONDecodeError, TypeError):
        result = None
    if not isinstance(result, dict):
        text = (output or "").strip()
        return {"is_error": True,
                "result": f"claude exited {returncode} without a JSON result: {text[:500]}"}
    if returncode != 0 and not result.get("is_error"):
        result["is_error"] = True
    return result


def probe_problem(returncode: int, output: str) -> str:
    """Judge the live probe. Empty string means the token works."""
    result = parse_claude_result(returncode, output)
    if result.get("is_error"):
        detail = str(result.get("result", ""))[:200]
        return ("CLAUDE_TOKEN was rejected by Anthropic (expired or revoked). "
                f"Run `claude setup-token` again and update the variable; {HANDBOOK}. "
                f"Claude said: {detail}")
    return ""


# --- statuses -----------------------------------------------------------------

def default_status(result: dict) -> dict:
    if result.get("is_error"):
        return {"status": "error",
                "message": str(result.get("result", "claude reported an error"))}
    return {"status": "ok", "message": ""}


def parse_check_output(output: str) -> dict:
    try:
        verdict = json.loads(output)
    except (json.JSONDecodeError, TypeError):
        return {"status": "error", "message": f"check hook printed no JSON: {output[:300]}"}
    if not isinstance(verdict, dict) or verdict.get("status") not in STATUSES:
        return {"status": "error",
                "message": f"check hook must print a status in {STATUSES}, got {output[:300]}"}
    return {"status": verdict["status"], "message": str(verdict.get("message", ""))}


def exit_code(statuses: list[str]) -> int:
    if "error" in statuses:
        return EXIT_ERROR
    if "needs_human" in statuses:
        return EXIT_NEEDS_HUMAN
    return EXIT_OK


# --- commands -----------------------------------------------------------------

def claude_command(task: Task, budget_usd: float | None = None) -> list[str]:
    command = [
        "claude", "-p",
        "--model", task.model,
        "--permission-mode", task.permission_mode,
        "--output-format", "json",
        "--max-budget-usd", str(budget_usd if budget_usd is not None else task.budget_usd),
    ]
    if task.allowed_tools:
        command += ["--allowedTools", ",".join(task.allowed_tools)]
    if task.disallowed_tools:
        command += ["--disallowedTools", ",".join(task.disallowed_tools)]
    if task.system is not None:
        command += ["--append-system-prompt-file", str(task.system)]
    return command


def run_claude(command: list[str], prompt: str, timeout: float, env) -> tuple[int, str, str]:
    """Run claude with the prompt on stdin. Returns (returncode, stdout, stderr)."""
    try:
        done = subprocess.run(
            command, input=prompt, capture_output=True, text=True,
            timeout=timeout, env=env, check=False)
    except FileNotFoundError:
        return 127, "", "claude is not installed or not on PATH"
    except subprocess.TimeoutExpired as expired:
        out = expired.stdout
        if isinstance(out, bytes):
            out = out.decode(errors="replace")
        return 124, out or "", f"claude ran longer than {int(timeout)} seconds and was stopped"
    return done.returncode, done.stdout, done.stderr


def run_hook(script: Path, stdin: str, env) -> tuple[int, str, str]:
    done = subprocess.run(
        [sys.executable, str(script)], input=stdin, capture_output=True, text=True,
        timeout=HOOK_TIMEOUT_SECONDS, env=env, check=False)
    return done.returncode, done.stdout, done.stderr


def claude_env(env) -> dict:
    """The environment Claude runs with: CLAUDE_TOKEN mapped to the name it reads."""
    out = dict(env)
    token = token_from(env)
    if token:
        out["CLAUDE_CODE_OAUTH_TOKEN"] = token
    return out


# --- orchestration ------------------------------------------------------------

def verify_token(task: Task, env, report_dir: Path) -> str:
    """Return "" when the token works, else the message for the log."""
    problem = missing_token_problem(env)
    if problem:
        return problem
    try:
        done = subprocess.run(["claude", "auth", "status", "--json"], capture_output=True,
                              text=True, timeout=60, env=env, check=False)
    except FileNotFoundError:
        return "claude is not installed or not on PATH; the CI job installs it in before_script"
    problem = auth_status_problem(done.stdout)
    if problem:
        return problem
    print(f"Claude Code is logged in as {auth_identity(done.stdout)}")
    print("Sending a probe prompt to confirm Anthropic accepts the token...")
    probe = Task(name="probe", folder=task.folder, prompt=PROBE_PROMPT, model=task.model)
    code, out, err = run_claude(claude_command(probe, PROBE_BUDGET_USD), PROBE_PROMPT,
                                PROBE_TIMEOUT_SECONDS, env)
    report_dir.mkdir(parents=True, exist_ok=True)
    record = parse_claude_result(code, out)
    record["stderr"] = err[-2000:]
    (report_dir / "auth-probe.json").write_text(json.dumps(record, indent=2))
    problem = probe_problem(code, out)
    if problem:
        return problem
    print("Token accepted.")
    return ""


def select_items(task: Task, env) -> list[dict]:
    if task.select is None:
        return [{}]
    code, out, err = run_hook(task.select, "", env)
    if err.strip():
        print(err.strip())
    if code != 0:
        raise TaskError(f"{task.select.name} exited {code}")
    try:
        items = json.loads(out)
    except json.JSONDecodeError:
        raise TaskError(f"{task.select.name} must print a JSON list, got: {out[:300]}") from None
    if not isinstance(items, list) or not all(isinstance(i, dict) for i in items):
        raise TaskError(f"{task.select.name} must print a JSON list of objects")
    return items


def check_item(task: Task, item: dict, result: dict, env) -> dict:
    if task.check is None:
        return default_status(result)
    payload = json.dumps({"item": item, "result": result, "job_url": env.get("CI_JOB_URL", "")})
    code, out, err = run_hook(task.check, payload, env)
    if err.strip():
        print(err.strip())
    if code != 0:
        return {"status": "error", "message": f"{task.check.name} exited {code}"}
    return parse_check_output(out)


def run_item(task: Task, item: dict, key: str, env, report_dir: Path) -> dict:
    prompt = render_prompt(task.prompt, item)
    print(f"--- {task.name} item {key}: starting claude ({task.model}, "
          f"budget ${task.budget_usd:g}, {task.timeout_minutes} min)")
    code, out, err = run_claude(claude_command(task), prompt, task.timeout_minutes * 60, env)
    result = parse_claude_result(code, out)
    if err.strip():
        result["stderr"] = err.strip()[-4000:]
    (report_dir / f"{key}.json").write_text(json.dumps({"item": item, "result": result}, indent=2))
    cost = result.get("total_cost_usd")
    turns = result.get("num_turns")
    print(f"    claude finished: exit {code}, cost ${cost if cost is not None else '?'}, "
          f"turns {turns if turns is not None else '?'}")
    verdict = check_item(task, item, result, env)
    print(f"    {verdict['status']}: {verdict['message'] or 'no message'}")
    return {"id": key, "item": item, **verdict, "cost_usd": cost, "turns": turns}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("task", help="folder name under ci/claude-tasks/")
    parser.add_argument("--dry-run", action="store_true",
                        help="verify the token and list the items, then stop")
    parser.add_argument("--only", help="run just the item whose id matches")
    args = parser.parse_args(argv)

    env = claude_env(os.environ)
    report_dir = REPORT_DIR / args.task
    try:
        task = load_task(args.task)
        problem = verify_token(task, env, report_dir)
        if problem:
            print(problem)
            return EXIT_ERROR
        items = select_items(task, env)
        if args.only is not None:
            items = [i for n, i in enumerate(items) if item_id(i, n) == args.only]
            if not items:
                raise TaskError(f"no item with id {args.only!r}")
        print(f"{len(items)} item(s) for task {task.name}")
        for n, item in enumerate(items):
            print(f"  {item_id(item, n)}: {json.dumps(item)[:200]}")
        if args.dry_run:
            print("dry run: nothing started")
            return EXIT_OK
        if not items:
            return EXIT_OK
        report_dir.mkdir(parents=True, exist_ok=True)
        outcomes = [run_item(task, item, item_id(item, n), env, report_dir)
                    for n, item in enumerate(items)]
    except TaskError as problem:
        print(str(problem))
        return EXIT_ERROR

    (report_dir / "summary.json").write_text(json.dumps(outcomes, indent=2))
    print(f"--- {task.name}: {len(outcomes)} item(s)")
    for outcome in outcomes:
        print(f"  {outcome['status']:11} {outcome['id']}  {outcome['message']}")
    code = exit_code([o["status"] for o in outcomes])
    if code == EXIT_NEEDS_HUMAN:
        print("some items need a person; the job ends with a warning")
    return code


if __name__ == "__main__":
    sys.exit(main())
