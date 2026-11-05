"""`python -m pdt.hosted_runner`: run one command for one place in one Fargate task.

The task's environment holds the command ID, the API URL, and a one-time claim token
(PDT_COMMAND_ID, PDT_API_URL, PDT_RUNNER_CLAIM). The runner removes them from its environment,
claims the command at `/v1/runner/claim`, and gets the operation, its args, its run limit, a run
token, one hour of credentials for the one place, the current copy of the project, and the command
input. It writes the copy into a new folder and runs pdt there as a child, as USER when it runs as
root, with an allow list of env vars and the credentials, and nothing else of its own environment.
It sends the output each OUTPUT_SECONDS to `/v1/runner/report`, stops the child when the answer
says `cancel`, sends AutoIDM each unit of the copy whose `pdt.yml` or `<app>/config.yml` the
child changed (`save_config`), uploads the log and, for `artifacts` and `run_mr`, the output folder, and reports
the result. A hard timer ends the process at the run limit plus GRACE_SECONDS. Exit code 0 means
that AutoIDM has the result.

A `run_mr` runs `python -m pdt.hosted_runner mr-run APP IID` as the child: it builds and runs the
job of the merge request through the provider verb `mr-run`, and copies that run's output folder
`runs/<time>-<command ID>/` for the artifact.
"""

from __future__ import annotations

import hashlib
import json
import os
import signal
import sys
import tempfile
import threading
import time
import zipfile
from pathlib import Path

from pdt import (config, console, copies, deploy, grants, managed_services, run_output,
                 service_commands, service_runner)
from pdt.deploy_common import KEEP_SECRET_ENV
from pdt.service_commands import OPERATIONS, Refused
from pdt.utils import autoidm

CLAIM_ENV = "PDT_RUNNER_CLAIM"
COMMAND_ENV = "PDT_COMMAND_ID"
OUTPUT_SECONDS = 10
GRACE_SECONDS = 300
# An `mr-run` deletes its cloud job and image after the SIGTERM of a cancel or of the run limit.
STOP_SECONDS = 120
TIMEOUT_EXIT = 124
ENV_KEEP = ("PATH", "PATHEXT", "HOME", "USERPROFILE", "SYSTEMROOT", "COMSPEC", "TEMP", "TMP",
            "TMPDIR", "LANG", "LC_ALL", "PROGRAMDATA", "LOCALAPPDATA", "APPDATA", "XDG_DATA_HOME",
            "XDG_CACHE_HOME", "UV_CACHE_DIR", "UV_PYTHON_INSTALL_DIR", "UV_TOOL_DIR",
            "UV_SYSTEM_CERTS", "SSL_CERT_FILE")
USER_PATHS = ("HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME", "UV_CACHE_DIR", "UV_PYTHON_INSTALL_DIR",
              "UV_TOOL_DIR")
KEEP_SECRET_OPERATIONS = ("deploy", "plan")
USER = "pdtjob"
MR_RUN = "mr-run"


class Runner:

    def __init__(self, api_url: str, command_id: str) -> None:
        self.api_url = api_url.rstrip("/")
        self.command_id = command_id
        self.run_token: str | None = None
        self.seq = 0

    def post(self, path: str, body: dict) -> dict:
        headers = {"Authorization": f"Bearer {self.run_token}"} if self.run_token else {}
        return autoidm.retried(lambda: autoidm.send(
            f"{self.api_url}{path}", json.dumps({"version": 1, **body}).encode(), headers))

    def report(self, **facts) -> dict:
        return self.post("/v1/runner/report", {"command_id": self.command_id, **facts})

    def upload(self, kind: str, filename: str, data: bytes) -> str:
        place = self.post("/v1/runner/uploads", {"command_id": self.command_id, "kind": kind,
                                                  "filename": filename, "size": len(data)})
        autoidm.post_file(place, filename, data, "/v1/runner/uploads")
        return place["key"]


def stop_at(seconds: float) -> None:
    def end() -> None:
        console.error(f"The hosted runner passed its time limit of {console.value(round(seconds))} "
                      "seconds.")
        console.drain()
        os._exit(TIMEOUT_EXIT)
    timer = threading.Timer(seconds, end)
    timer.daemon = True
    timer.start()


def checked(answer: dict) -> tuple[str, dict, int]:
    operation, args = answer.get("operation"), answer.get("args")
    if not isinstance(operation, str) or operation not in OPERATIONS:
        raise Refused(f"pdt does not know the operation {operation!r}.")
    most = OPERATIONS[operation].runner_limit_seconds
    if most is None:
        raise Refused(f"The hosted runner does not run {operation}.")
    problem = service_commands.args_problem(operation, args)
    if problem:
        raise Refused(f"The {operation} command was refused: {problem}.")
    if operation == "run" and not args["deployed"]:
        raise Refused("The hosted runner runs only the deployed job.")
    limit = answer.get("limit_seconds")
    if not (type(limit) is int and limit > 0):
        raise Refused("The command has no run limit.")
    if not service_commands.PLACE.fullmatch(str(answer.get("place"))):
        raise Refused(f"The command names no place: {answer.get('place')!r}.")
    return operation, args, min(limit, most)


def write_copy(answer: dict, project: Path) -> None:
    for entry in (answer.get("copy") or {}).get("units") or []:
        unit = entry.get("unit") if isinstance(entry, dict) else None
        if not (isinstance(unit, str) and copies.UNIT.fullmatch(unit)):
            raise Refused(f"The copy names the unit {unit!r}.")
        copies.fetch(entry, project)


def child_env(base: dict[str, str], project: Path, credentials: dict[str, str],
              extra: dict[str, str]) -> dict[str, str]:
    wanted = {name.upper() for name in ENV_KEEP}
    kept = {name: value for name, value in base.items() if name.upper() in wanted}
    # A copy holds customer files, so a uv.toml in one must not choose a package index.
    return {**kept, **credentials, **extra, "PDT_PROJECT": str(project), "PYTHONUTF8": "1",
            "UV_NO_CONFIG": "1", "CI": "pdt-hosted-runner",
            config.SERVICE_CHILD_ENV: "1", config.SERVICE_ENV_FILES_ENV: ""}


def run_user() -> str | None:
    return USER if os.name != "nt" and os.geteuid() == 0 else None


def user_home(user: str) -> str:
    import pwd
    return pwd.getpwnam(user).pw_dir


def give_folder(folder: Path, user: str) -> None:
    import pwd
    uid = pwd.getpwnam(user).pw_uid
    for parent, dirs, files in os.walk(folder):
        os.chown(parent, uid, -1)
        for name in [*dirs, *files]:
            os.chown(Path(parent, name), uid, -1, follow_symlinks=False)


def zipped_folder(folder: Path) -> bytes | None:
    files = [path for path in sorted(folder.rglob("*")) if path.is_file()] if folder.is_dir() else []
    if not files:
        return None
    archive = folder.with_name("artifacts.zip")
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED, strict_timestamps=False) as out:
        for path in files:
            out.write(path, path.relative_to(folder).as_posix())
    return archive.read_bytes()


def send_output(runner: Runner, log: Path, offset: int, secrets: list[bytes],
                final: bool) -> tuple[int, bool]:
    cancel = False
    while True:
        with log.open("rb") as handle:
            handle.seek(offset)
            data = handle.read(service_runner.OUTPUT_LIMIT * 2)
        text, used = service_runner.next_chunk(data, secrets, final)
        if used == 0 and final:
            return offset, cancel
        answer = runner.report(fact="output", seq=runner.seq, text=text)
        runner.seq += 1
        offset += used
        cancel = cancel or answer.get("cancel") is True
        if not final:
            return offset, cancel


def run(runner: Runner, answer: dict, work: Path) -> dict:
    operation, args, limit = checked(answer)
    stop_at(limit + GRACE_SECONDS)
    project = work / "project"
    project.mkdir()
    write_copy(answer, project)
    if operation == "run_mr":
        units = (answer.get("copy") or {}).get("units") or [{}]
        problem = service_runner.mr_copy_problem(units[0], project, args["app"], args["sha"])
        if problem:
            raise Refused(problem)
    (project / config.PROJECT_NAME_FILE).write_text(args["project"] + "\n", encoding="utf-8")
    app = args.get("app")
    if app:
        state = project / config.STATE_DIR / "state"
        state.parent.mkdir(exist_ok=True)
        state.write_text(json.dumps({"deployed": [app]}) + "\n", encoding="utf-8")
    credentials = grants.credentials(answer["place"], answer.get("credentials") or {})
    extra = {"PDT_RUN_ID": runner.command_id}
    output = work / "output"
    argv = service_commands.command_line(operation, args)
    if operation == "artifacts":
        argv.append(str(output))
    if operation == "run_mr":
        argv = [sys.executable, "-m", "pdt.hosted_runner", MR_RUN, args["app"], args["mr_iid"]]
        extra["PDT_OUTPUT_DIR"] = str(output)
    output.mkdir()
    if "app_args" in args:
        extra["PDT_APP_ARGS"] = args["app_args"]
    if operation in KEEP_SECRET_OPERATIONS:
        extra[KEEP_SECRET_ENV] = "1"
    if "timezone" in args:
        extra[deploy.TIMEZONE_ENV] = args["timezone"]
    if operation == "deploy":
        extra[autoidm.RUNNER_ENV] = json.dumps({"api_url": runner.api_url, "command_id": runner.command_id,
                                                "run_token": runner.run_token})
    value = (answer.get("input") or {}).get("value")
    base = dict(os.environ)
    user = run_user()
    if user:
        give_folder(work, user)
        if grants.GRANT_FOLDER_ENV in credentials:
            give_folder(Path(credentials[grants.GRANT_FOLDER_ENV]), user)
        base = {name: text for name, text in base.items() if name not in USER_PATHS}
        base["HOME"] = user_home(user)
    log = work / "output.log"
    secrets = service_runner.secret_values([value] if isinstance(value, str) else [])
    units = [] if operation == "run_mr" else (answer.get("copy") or {}).get("units") or []
    before = read_config(project, units)
    try:
        code, outcome, message = run_child(runner, argv, project, child_env(
            base, project, credentials, extra), log, value, user, limit, secrets)
    finally:
        service_runner.remove_folder(credentials.get(grants.GRANT_FOLDER_ENV))
    problem = save_config(runner, project, units, before, log)
    message = message or problem
    size = log.stat().st_size if log.is_file() else 0
    upload = size <= managed_services.MAX_UPLOAD_BYTES
    keep = size if upload else service_runner.MAX_MESSAGE + max(map(len, secrets), default=0)
    data = b""
    if size:
        with log.open("rb") as handle:
            handle.seek(size - keep)
            data = service_runner.mask(handle.read(keep), secrets)
    if outcome is None:
        outcome = "done" if code == 0 else "failed"
        if outcome == "failed":
            message = data[-service_runner.MAX_MESSAGE:].decode("utf-8", "replace").strip()
    log_key = artifact_key = None
    if data and upload:
        log_key = runner.upload("log", f"{runner.command_id}.log", data)
    if (operation, outcome) in (("artifacts", "done"), ("run_mr", "done"), ("run_mr", "failed")):
        zipped = zipped_folder(output)
        if zipped is not None and len(zipped) <= managed_services.MAX_UPLOAD_BYTES:
            artifact_key = runner.upload("artifact", service_runner.ARTIFACT_NAME, zipped)
    return service_runner.result_of(outcome, code, message, runner.command_id, artifact_key,
                                    log_key)


def config_path(unit: str) -> str:
    return "pdt.yml" if unit == copies.ROOT else f"{unit.removeprefix('app=')}/config.yml"


def read_config(project: Path, units: list[dict]) -> dict[str, bytes | None]:
    found = {}
    for entry in units:
        path = project / config_path(entry["unit"])
        found[entry["unit"]] = path.read_bytes() if path.is_file() else None
    return found


def save_config(runner: Runner, project: Path, units: list[dict],
                before: dict[str, bytes | None], log: Path) -> str | None:
    """Send AutoIDM each unit of the copy whose config file pdt changed in the run, such as the
    time zone a first deploy saves, as pdt run by a person keeps it in the project folder. The
    copy must still be the one the runner got, so a change made in the meantime stays. Returns
    the problem when a unit could not be saved."""
    problem = None
    for entry in units:
        unit, relative = entry["unit"], config_path(entry["unit"])
        path = project / relative
        text = path.read_bytes() if path.is_file() else None
        if text == before[unit]:
            continue
        try:
            paths = {item["path"] for item in entry["files"]} - {relative}
            if text is not None:
                paths.add(relative)
            files = [{"path": name, "size": (project / name).stat().st_size}
                     for name in sorted(paths)]
            data = copies.build_zip(project, files)
            digest = hashlib.sha256(data).hexdigest()
            body = {"command_id": runner.command_id, "unit": unit, "sha256": digest}
            answer = runner.post("/v1/runner/copies/start", {
                **body, "size": len(data), "files": files, "base_sha256": entry["sha256"]})
            if answer.get("upload"):
                copies.put(answer["upload"], data, digest)
            runner.post("/v1/runner/copies/finish", body)
            line = f"pdt saved {relative} in the AutoIDM copy of the project."
        except (copies.CopyError, autoidm.AutoidmError, OSError) as e:
            line = f"pdt could not save {relative} in the AutoIDM copy of the project: {e}"
            problem = problem or line
        with log.open("a", encoding="utf-8") as out:
            out.write(line + "\n")
    return problem


def run_child(runner: Runner, argv: list[str], project: Path, env: dict[str, str], log: Path,
              value: str | None, user: str | None, limit: int,
              secrets: list[bytes]) -> tuple[int | None, str | None, str | None]:
    import subprocess
    options = dict(service_runner.tree_options())
    if user:
        options["user"] = user
    with log.open("ab") as out:
        proc = subprocess.Popen(argv, cwd=project, env=env, stdout=out, stderr=subprocess.STDOUT,
                                stdin=subprocess.PIPE if value is not None else subprocess.DEVNULL,
                                **options)
    if value is not None:
        proc.stdin.write(value.encode())
        proc.stdin.close()
    runner.report(fact="started")
    deadline = time.monotonic() + limit
    offset, outcome, message = 0, None, None
    next_output = time.monotonic() + OUTPUT_SECONDS
    while proc.poll() is None:
        cancel = False
        if time.monotonic() >= next_output:
            offset, cancel = send_output(runner, log, offset, secrets, False)
            next_output = time.monotonic() + OUTPUT_SECONDS
        if cancel:
            outcome, message = "cancelled", "A person cancelled the command."
        elif time.monotonic() >= deadline:
            outcome, message = "failed", f"The command passed its run limit of {limit} seconds."
        if outcome is not None:
            service_runner.stop_tree(proc.pid, lambda: proc.poll() is not None, STOP_SECONDS)
            proc.wait()
            break
        time.sleep(0.2)
    send_output(runner, log, offset, secrets, True)
    return proc.returncode, outcome, message


def mr_run(app: str, iid: str) -> int:
    # The provider script cleans up after the SIGTERM of the group; this process waits for it.
    # A handler, unlike SIG_IGN, does not pass on to the programs this process starts.
    signal.signal(signal.SIGTERM, lambda _signum, _frame: None)
    code = deploy.mr_run(app, iid)
    try:
        run_output.copy_run(app, os.environ["PDT_RUN_ID"], Path(os.environ["PDT_OUTPUT_DIR"]))
    except (RuntimeError, ValueError, KeyError) as e:
        console.warn(f"pdt could not copy the output of the run: {console.escape(str(e))}")
    return code


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == [MR_RUN] and len(argv) == 3:
        return mr_run(argv[1], argv[2])
    claim = os.environ.pop(CLAIM_ENV, "").strip()
    api_url = os.environ.pop(autoidm.URL_ENV, "").strip()
    command_id = os.environ.pop(COMMAND_ENV, "").strip()
    if not (claim and api_url and service_commands.COMMAND_ID.fullmatch(command_id)):
        console.error(f"{console.value(CLAIM_ENV)}, {console.value(autoidm.URL_ENV)}, and "
                      f"{console.value(COMMAND_ENV)} must be set.")
        return 2
    runner = Runner(autoidm.check_url(api_url, COMMAND_ENV), command_id)
    try:
        answer = runner.post("/v1/runner/claim", {"command_id": command_id, "claim_token": claim})
    except autoidm.AutoidmError as e:
        console.error(console.escape(str(e)))
        return 0 if e.code in ("already_claimed", "cancelled", "claim_expired") else 1
    runner.run_token = answer.get("run_token")
    with tempfile.TemporaryDirectory(prefix="pdt-runner-") as folder:
        try:
            result = run(runner, answer, Path(folder))
        except Refused as e:
            result = service_runner.result_of("refused", message=str(e))
        except (copies.CopyError, autoidm.AutoidmError, OSError, RuntimeError, KeyError,
                ValueError) as e:
            result = service_runner.result_of("failed", message=str(e)[:service_runner.MAX_MESSAGE])
    try:
        runner.report(fact="result", **result)
    except autoidm.AutoidmError as e:
        console.error(f"AutoIDM does not have the result of {console.value(command_id)}: "
                      f"{console.escape(str(e))}")
        return 1
    console.say(f"{console.value(command_id)}: {console.value(result['outcome'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
