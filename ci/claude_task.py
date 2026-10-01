"""Run one Claude Code task from GitLab CI on the team's Claude subscription.

A task is a folder under ci/claude-tasks/<name>/:

  task.yml    model, effort, budget_usd, timeout_minutes, permission_mode,
              allowed_tools, disallowed_tools, parallel (how many items run
              at once, default 1), and the optional hook filenames
              ``select`` and ``check``.
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

Each item runs in its own git worktree, a detached copy of the checkout at
HEAD, so items can run side by side without stepping on each other's
branches. Claude and the check hook get that worktree as their working
folder; it is deleted when the item is done. The select hook runs once in
the main checkout before any item starts, and check hooks run one at a time
because they fetch into the shared .git.

Usage: ci/claude_task.py <task-name> [--dry-run] [--only ID]

A dry run sets CLAUDE_TASK_DRY_RUN=1 for the select hook, so a hook with
side effects can only report.

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
import shutil
import subprocess
import sys
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

import yaml

TASKS_DIR = Path(__file__).resolve().parent / "claude-tasks"
REPORT_DIR = Path("claude-task-report")
HANDBOOK = "steps are in the handbook (developer.md, Claude Code in CI)"
PROBE_PROMPT = "Reply with the single word OK and nothing else."
# The probe loads the repo's CLAUDE.md and AGENTS.md like any session, which
# alone is about $0.12 of Opus cache writes at list price. A cap of $0.10
# made a working token look broken (subtype error_max_budget_usd).
PROBE_BUDGET_USD = 1.00
PROBE_TIMEOUT_SECONDS = 120
HOOK_TIMEOUT_SECONDS = 600
STATUSES = ("ok", "needs_human", "error")
EXIT_OK, EXIT_ERROR, EXIT_NEEDS_HUMAN = 0, 1, 2
TOKEN_NAMES = ("CLAUDE_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN")
# "oauth_token" is what `claude auth status` reports for a token taken from the
# environment (the CI case); "claude.ai" is an interactive login on a laptop.
SUBSCRIPTION_AUTH_METHODS = ("oauth_token", "claude.ai")
KNOWN_KEYS = {"model", "effort", "budget_usd", "timeout_minutes", "permission_mode",
              "allowed_tools", "disallowed_tools", "parallel", "select", "check"}
# Hooks fetch into the shared .git, and two fetches of the same ref at once
# fail with "cannot lock ref". Worktree add/remove edits .git too.
GIT_LOCK = threading.Lock()


class TaskError(Exception):
    """A message for the job log. It names the file or variable to fix."""


@dataclass
class Task:
    name: str
    folder: Path
    prompt: str
    model: str = "opus"
    effort: str = ""
    budget_usd: float = 20.0
    timeout_minutes: int = 30
    permission_mode: str = "acceptEdits"
    allowed_tools: list[str] = field(default_factory=list)
    disallowed_tools: list[str] = field(default_factory=list)
    parallel: int = 1
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
    if "effort" in raw:
        task.effort = _text(config, "effort", raw["effort"])
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
    if "parallel" in raw:
        task.parallel = int(_number(config, "parallel", raw["parallel"]))
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
    return f"CLAUDE_TOKEN is not set; {HANDBOOK}."


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


def probe_problem(returncode: int, output: str, stderr: str = "") -> str:
    """Judge the live probe. Empty string means the token works.

    On failure the message carries what Claude Code itself reported, with
    no guess about the cause: the result text when there is one, else the
    error subtype, plus anything on stderr.
    """
    result = parse_claude_result(returncode, output)
    if not result.get("is_error"):
        return ""
    said = str(result.get("result") or result.get("subtype") or "no message")
    if stderr.strip():
        said += f"\n{stderr.strip()[-1000:]}"
    return f"The probe prompt failed with CLAUDE_TOKEN. Claude Code said: {said}"


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
    if task.effort:
        command += ["--effort", task.effort]
    if task.allowed_tools:
        command += ["--allowedTools", ",".join(task.allowed_tools)]
    if task.disallowed_tools:
        command += ["--disallowedTools", ",".join(task.disallowed_tools)]
    if task.system is not None:
        command += ["--append-system-prompt-file", str(task.system)]
    return command


def run_claude(command: list[str], prompt: str, timeout: float, env,
               cwd: Path | None = None) -> tuple[int, str, str]:
    """Run claude with the prompt on stdin. Returns (returncode, stdout, stderr)."""
    try:
        done = subprocess.run(
            command, input=prompt, capture_output=True, text=True,
            timeout=timeout, env=env, cwd=cwd, check=False)
    except FileNotFoundError:
        return 127, "", "claude is not installed or not on PATH"
    except subprocess.TimeoutExpired as expired:
        out = expired.stdout
        if isinstance(out, bytes):
            out = out.decode(errors="replace")
        return 124, out or "", f"claude ran longer than {int(timeout)} seconds and was stopped"
    return done.returncode, done.stdout, done.stderr


def run_hook(script: Path, stdin: str, env, cwd: Path | None = None) -> tuple[int, str, str]:
    with GIT_LOCK:
        done = subprocess.run(
            [sys.executable, str(script)], input=stdin, capture_output=True, text=True,
            timeout=HOOK_TIMEOUT_SECONDS, env=env, cwd=cwd, check=False)
    return done.returncode, done.stdout, done.stderr


def claude_env(env, cwd: Path | None = None) -> dict:
    """The environment Claude and the hooks run with.

    CLAUDE_TOKEN is mapped to the name Claude Code reads. A relative
    UV_CACHE_DIR (the CI job sets .uv-cache) is made absolute against ``cwd``
    so `uv run` inside a per-item worktree still hits the job's cache.
    """
    out = dict(env)
    token = token_from(env)
    if token:
        out["CLAUDE_CODE_OAUTH_TOKEN"] = token
    cache = out.get("UV_CACHE_DIR", "")
    if cache and not os.path.isabs(cache):
        out["UV_CACHE_DIR"] = str((Path(cwd) if cwd else Path.cwd()).resolve() / cache)
    return out


# --- per-item worktrees -------------------------------------------------------

def add_worktree(key: str) -> Path:
    """A detached copy of the checkout at HEAD, in a temp folder, for one item."""
    path = Path(tempfile.mkdtemp(prefix=f"claude-task-{key}-")) / "repo"
    with GIT_LOCK:
        done = subprocess.run(["git", "worktree", "add", "--quiet", "--detach", str(path), "HEAD"],
                              capture_output=True, text=True, check=False)
    if done.returncode != 0:
        shutil.rmtree(path.parent, ignore_errors=True)
        raise TaskError(f"git worktree add failed for item {key}: {done.stderr.strip()}")
    return path


def remove_worktree(path: Path) -> None:
    with GIT_LOCK:
        subprocess.run(["git", "worktree", "remove", "--force", str(path)],
                       capture_output=True, text=True, check=False)
    shutil.rmtree(path.parent, ignore_errors=True)


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
    problem = probe_problem(code, out, err)
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


def check_item(task: Task, item: dict, result: dict, env, cwd: Path | None = None) -> dict:
    if task.check is None:
        return default_status(result)
    payload = json.dumps({"item": item, "result": result, "job_url": env.get("CI_JOB_URL", "")})
    code, out, err = run_hook(task.check, payload, env, cwd)
    if err.strip():
        print(err.strip())
    if code != 0:
        return {"status": "error", "message": f"{task.check.name} exited {code}"}
    return parse_check_output(out)


def run_item(task: Task, item: dict, key: str, env, report_dir: Path) -> dict:
    """One Claude session plus its check, in a worktree of its own.

    Items run side by side, so every line names the item.
    """
    prompt = render_prompt(task.prompt, item)
    print(f"--- {task.name} item {key}: starting claude ({task.model}, "
          f"budget ${task.budget_usd:g}, {task.timeout_minutes} min)")
    worktree = add_worktree(key)
    try:
        item_env = claude_env(env)
        code, out, err = run_claude(claude_command(task), prompt, task.timeout_minutes * 60,
                                    item_env, worktree)
        result = parse_claude_result(code, out)
        if err.strip():
            result["stderr"] = err.strip()[-4000:]
        (report_dir / f"{key}.json").write_text(
            json.dumps({"item": item, "result": result}, indent=2))
        cost = result.get("total_cost_usd")
        turns = result.get("num_turns")
        seconds = int(result.get("duration_ms") or 0) // 1000
        print(f"--- {task.name} item {key}: claude finished in {seconds} s, exit {code}, "
              f"cost ${cost if cost is not None else '?'}, "
              f"turns {turns if turns is not None else '?'}")
        verdict = check_item(task, item, result, item_env, worktree)
    finally:
        remove_worktree(worktree)
    print(f"--- {task.name} item {key}: {verdict['status']}: {verdict['message'] or 'no message'}")
    return {"id": key, "item": item, **verdict, "cost_usd": cost, "turns": turns}


def run_items(task: Task, items: list[dict], env, report_dir: Path) -> list[dict]:
    """Run every item, ``task.parallel`` at a time. Outcomes keep the item order."""
    keys = [item_id(item, n) for n, item in enumerate(items)]
    with ThreadPoolExecutor(max_workers=max(1, task.parallel)) as pool:
        return list(pool.map(lambda pair: run_item(task, pair[0], pair[1], env, report_dir),
                             zip(items, keys)))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("task", help="folder name under ci/claude-tasks/")
    parser.add_argument("--dry-run", action="store_true",
                        help="verify the token and list the items, then stop")
    parser.add_argument("--only", help="run just the item whose id matches")
    args = parser.parse_args(argv)

    # The job log is a pipe, so without this Python holds output back until
    # the job ends and a running job shows nothing.
    sys.stdout.reconfigure(line_buffering=True)
    env = claude_env(os.environ)
    report_dir = REPORT_DIR / args.task
    try:
        task = load_task(args.task)
        problem = verify_token(task, env, report_dir)
        if problem:
            print(problem)
            return EXIT_ERROR
        hook_env = {**env, "CLAUDE_TASK_DRY_RUN": "1"} if args.dry_run else env
        items = select_items(task, hook_env)
        if args.only is not None:
            items = [i for n, i in enumerate(items) if item_id(i, n) == args.only]
            if not items:
                raise TaskError(f"no item with id {args.only!r}")
        print(f"{len(items)} item(s) for task {task.name}, up to {task.parallel} at a time")
        for n, item in enumerate(items):
            print(f"  {item_id(item, n)}: {json.dumps(item)[:200]}")
        if args.dry_run:
            print("dry run: nothing started")
            return EXIT_OK
        if not items:
            return EXIT_OK
        report_dir.mkdir(parents=True, exist_ok=True)
        outcomes = run_items(task, items, env, report_dir)
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
