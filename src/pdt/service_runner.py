"""How `pdt service` runs one command: its journal entry, its child process, and its reports.

Each command is a child process of pdt, started with a list of arguments and no shell, in a new
process group on POSIX and with no window on Windows. The child writes its output to the file
`<data folder>/service/output/<command_id>.log`, and the service reads that file, so output
survives a restart of the service. The journal moves `output_offset` and `output_seq` only after
AutoIDM answers 2xx, so a failed send goes again. Before output leaves the computer, `mask`
replaces each known secret value, and a chunk never ends inside one.

A cancel, the run limit, or a stop of the service ends the whole process tree. On Windows each
child starts suspended in a job object of its own, which ends the tree when the service cancels
it or when the service ends for any reason. At most MAX_RUNNING
commands run at once. A command that conflicts with a running one waits in the Pool. A `kill`
conflicts with no command, and it has one more place than MAX_RUNNING. A lock of
another process gives the outcome `refused`. The result goes to AutoIDM only after the output, and
each report is one request, so a busy AutoIDM does not hold up a cancel or the run limit.

The journal is one file per command in `<data folder>/service/journal/`. Its state is `claiming`,
`claimed`, `started`, or `finished`. `claiming` holds a command whose claim got no answer, and the
check-in names it so AutoIDM sends the token again. A file that does not parse refuses that
command, and the service keeps the file. At a start, `recover` follows each process that still
runs (both the PID and the creation time match), and reports `interrupted` for the others.

`run_mr` runs the app from the copy of a merge request in a work folder of its own, so it takes
the locks of that folder and none of the project on this computer. `secrets_set` gets its value
from `/v1/commands/input` once and gives it to the child on stdin.
"""

from __future__ import annotations

import codecs
import json
import os
import re
import shutil
import signal
import subprocess
import tempfile
import threading
import time
import tomllib
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import jwt
from dotenv import dotenv_values

from pdt import config, copies, managed_services, run_windows_task, service_commands
from pdt.service_commands import OPERATIONS, Clock, Refused
from pdt.utils import autoidm

JOURNAL_SCHEMA = 1
MAX_RUNNING = 4
OUTPUT_SECONDS = 10
STOP_SECONDS = 10
MAX_MESSAGE = 4096
OUTPUT_LIMIT = 200 * 1024
OUTPUT_KEEP_SECONDS = 7 * 24 * 3600
MIN_SECRET_CHARACTERS = 6
SECRET_MASK = b"***"
TIME_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
SERVICE_PROJECT_ENV = "PDT_SERVICE_PROJECT"
SERVICE_CUSTOMER_ENV = "PDT_SERVICE_CUSTOMER"
PROJECTS_HOME_ENV = "PDT_SERVICE_PROJECTS"
ARTIFACT_NAME = "artifacts.zip"
EVENT_SOURCE = "pdt-service"
EVENT_IDS = {"claimed": 1000, "started": 1001, "result": 1002}
EVENTLOG_INFORMATION_TYPE = 4
NOT_FOR_A_CHILD = set(autoidm.SECRET_NAMES)
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
CREATE_SUSPENDED = 0x4
STILL_ACTIVE = 259
# The first pdt-cli whose storage keeps the state of a merge request run apart from the state of
# the deployed job. An app runs with the pdt-cli its run.py pins.
MR_STATE_VERSION = "0.1.14"
SCRIPT_BLOCK = re.compile(r"(?m)^# /// script[ \t]*\n((?:#(?: .*)?\n)+?)# ///[ \t]*$")
PDT_REQUIREMENT = re.compile(r"\s*pdt[-_.]cli\s*(?:\[[^\]]*\])?\s*([<>=!~;@].*)?", re.I)
# The bounds that can hold an app below MR_STATE_VERSION.
UPPER_BOUND = re.compile(r"(===|==|<=|<)\s*([0-9][^\s,;]*)")


def now_text() -> str:
    return datetime.now(timezone.utc).strftime(TIME_FORMAT)


def service_folder() -> Path:
    return autoidm.data_folder() / "service"


def journal_path(command_id: str) -> Path:
    return service_folder() / "journal" / f"{command_id}.json"


def output_path(command_id: str) -> Path:
    return service_folder() / "output" / f"{command_id}.log"


def journal_read(command_id: str) -> dict | None:
    try:
        text = journal_path(command_id).read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    found = json.loads(text)
    if not isinstance(found, dict):
        raise ValueError(f"{journal_path(command_id)} does not hold an object")
    return found


def journal_write(entry: dict) -> None:
    path = journal_path(entry["command_id"])
    path.parent.mkdir(parents=True, exist_ok=True)
    config.write_text_atomically(path, json.dumps(entry) + "\n")


def journal_update(command_id: str, **fields) -> None:
    journal_write({**journal_read(command_id), **fields})


def journal_entries() -> list[dict]:
    entries = []
    for path in sorted((service_folder() / "journal").glob("cmd_*.json")):
        try:
            found = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(found, dict) and found.get("command_id"):
            entries.append(found)
    return entries


def claiming_ids() -> list[str]:
    return [entry["command_id"] for entry in journal_entries() if entry.get("state") == "claiming"]


def write_event(command_id: str, fact: str, operation: str, requested_by: object,
                outcome: str | None) -> None:
    line = (f"command={command_id} fact={fact} operation={operation} "
            f"requested_by={requested_by} outcome={outcome or '-'}")
    write_log_line(line, EVENT_IDS[fact])


def write_log_line(line: str, event_id: int) -> None:
    try:
        if os.name == "nt":
            import ctypes
            advapi = ctypes.WinDLL("advapi32")
            advapi.RegisterEventSourceW.restype = ctypes.c_void_p
            advapi.ReportEventW.argtypes = (
                ctypes.c_void_p, ctypes.c_ushort, ctypes.c_ushort, ctypes.c_ulong, ctypes.c_void_p,
                ctypes.c_ushort, ctypes.c_ulong, ctypes.POINTER(ctypes.c_wchar_p), ctypes.c_void_p)
            advapi.DeregisterEventSource.argtypes = (ctypes.c_void_p,)
            source = advapi.RegisterEventSourceW(None, EVENT_SOURCE)
            if source:
                advapi.ReportEventW(source, EVENTLOG_INFORMATION_TYPE, 0, event_id, None, 1, 0,
                                    (ctypes.c_wchar_p * 1)(line), None)
                advapi.DeregisterEventSource(source)
        else:
            import syslog
            syslog.openlog(EVENT_SOURCE, 0, syslog.LOG_DAEMON)
            syslog.syslog(line)
    except (AttributeError, OSError):
        pass


def process_runs(pid: int) -> bool:
    return process_created(pid) is not None


def process_created(pid: int) -> str | None:
    """The time the OS started `pid`, as text the OS gives each time in the same form, or None
    when no such process runs. A PID alone does not name a process, because the OS uses it again."""
    if os.name == "nt":
        return windows_process_created(pid)
    proc = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)], stdin=subprocess.DEVNULL,
                          capture_output=True, text=True, env={**os.environ, "LC_ALL": "C"},
                          check=False)
    return proc.stdout.strip() or None


def windows_process_created(pid: int) -> str | None:
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel.GetProcessTimes.argtypes = (wintypes.HANDLE, *[ctypes.POINTER(wintypes.FILETIME)] * 4)
    kernel.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    handle = kernel.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return None
    try:
        code = wintypes.DWORD()
        if not kernel.GetExitCodeProcess(handle, ctypes.byref(code)) or code.value != STILL_ACTIVE:
            return None
        created, ended, kernel_time, user_time = (wintypes.FILETIME() for _ in range(4))
        if not kernel.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(ended),
                                      ctypes.byref(kernel_time), ctypes.byref(user_time)):
            return None
        return str((created.dwHighDateTime << 32) | created.dwLowDateTime)
    finally:
        kernel.CloseHandle(handle)


def is_same_process(pid: object, created: object) -> bool:
    return (type(pid) is int and isinstance(created, str) and created != ""
            and process_created(pid) == created)


def stop_tree(pid: int, finished, seconds: float = STOP_SECONDS) -> None:
    """End the process `pid` and each process it started. POSIX gets a stop request first and
    `seconds` to act on it. Windows has no such request for a process with no console."""
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                       stdin=subprocess.DEVNULL, capture_output=True, check=False)
        return
    try:
        os.killpg(pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline and not finished():
        time.sleep(0.1)
    try:
        os.killpg(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def tree_options() -> dict:
    if os.name == "nt":
        return {"creationflags": (subprocess.CREATE_NEW_PROCESS_GROUP
                                  | subprocess.CREATE_NO_WINDOW)}
    return {"start_new_session": True}


def child_env(base: dict[str, str], project: Path | None,
              extra: dict[str, str]) -> dict[str, str]:
    env = {name: value for name, value in base.items() if name not in NOT_FOR_A_CHILD}
    env["PYTHONUTF8"] = "1"
    env[config.SERVICE_CHILD_ENV] = "1"
    env.pop(config.LOCK_PROJECT_ENV, None)
    if project is not None:
        env["PDT_PROJECT"] = str(project)
    return {**env, **extra}


def env_files(project: Path | None, app: str | None) -> list[Path]:
    if project is None:
        return []
    folders = [project / app, project] if app else [project]
    return [folder / ".env" for folder in folders if (folder / ".env").is_file()]


def env_file_values(project: Path | None, app: str | None) -> dict[str, str]:
    values: dict[str, str] = {}
    for path in env_files(project, app):
        for name, value in dotenv_values(path, encoding="utf-8-sig").items():
            values.setdefault(name, value or "")
    return values


def secret_values(texts) -> list[bytes]:
    found = {text.encode() for text in texts if text and len(text) >= MIN_SECRET_CHARACTERS}
    return sorted(found, key=len, reverse=True)


def mask(data: bytes, secrets: list[bytes]) -> bytes:
    for secret in secrets:
        data = data.replace(secret, SECRET_MASK)
    return data


def safe_cut(data: bytes, cut: int, secrets: list[bytes], final: bool) -> int:
    for secret in secrets:
        start = data.find(secret, max(0, cut - len(secret) + 1))
        if start != -1 and start < cut:
            cut = start
        if not final and cut == len(data):
            for size in range(min(len(secret) - 1, len(data)), 0, -1):
                if data.endswith(secret[:size]):
                    cut = len(data) - size
                    break
    return cut


def next_chunk(data: bytes, secrets: list[bytes] = (), final: bool = True) -> tuple[str, int]:
    size = OUTPUT_LIMIT
    while True:
        cut = safe_cut(data, min(size, len(data)), secrets, final or size < len(data))
        if cut == 0:
            return "", 0
        decoder = codecs.getincrementaldecoder("utf-8")("replace")
        decoder.decode(data[:cut])
        used = cut - len(decoder.getstate()[0])
        if used <= 0:
            if not final:
                return "", 0
            used = cut
        text = mask(data[:used], secrets).decode("utf-8", "replace")
        if len(json.dumps(text)) <= OUTPUT_LIMIT:
            return text, used
        size //= 2


@dataclass
class Job:
    command_id: str
    claims: dict
    registration: autoidm.Registration
    project: Path | None
    clock: Clock = field(default_factory=Clock)
    known_values: list[str] = field(default_factory=list)
    cancel: threading.Event = field(default_factory=threading.Event)
    interrupt: threading.Event = field(default_factory=threading.Event)
    thread: threading.Thread | None = None
    offset: int = 0
    seq: int = 0
    adopted_pid: int | None = None
    output_waits: bool = False

    @property
    def operation(self) -> str:
        return self.claims["operation"]

    @property
    def args(self) -> dict:
        return self.claims["args"]

    @property
    def exclusive(self) -> bool:
        return OPERATIONS[self.operation].exclusive

    def conflicts_with(self, other: "Job") -> bool:
        if self.args["project"] != other.args["project"]:
            return False
        if "kill" in (self.operation, other.operation):
            return False
        return (self.exclusive or other.exclusive
                or self.args.get("app") == other.args.get("app"))


def report(registration: autoidm.Registration, command_id: str, **facts) -> dict:
    return autoidm.post(registration, "/v1/commands/report",
                        {"version": 1, "command_id": command_id, **facts}, retry=False)


def sent_again_later(error: autoidm.AutoidmError) -> bool:
    return error.status in (0, 401, 429) or error.status >= 500


def send_result(registration: autoidm.Registration, command_id: str, result: dict) -> bool:
    message = result.get("message")
    sent = {**result, "message": message[:MAX_MESSAGE] if message else None}
    try:
        report(registration, command_id, fact="result", **sent)
    except autoidm.AutoidmError as e:
        if sent_again_later(e):
            return False
    if journal_path(command_id).is_file():
        try:
            journal_update(command_id, reported=True)
        except ValueError:
            pass
    return True


def result_of(outcome: str, exit_code: int | None = None, message: str | None = None,
              run_id: str | None = None, artifact_key: str | None = None,
              log_key: str | None = None) -> dict:
    return {"outcome": outcome, "exit_code": exit_code, "message": message, "run_id": run_id,
            "artifact_key": artifact_key, "log_key": log_key}


def finish(job: Job, result: dict) -> None:
    journal_update(job.command_id, state="finished", finished_at=now_text(), result=result,
                   reported=False)
    write_event(job.command_id, "result", job.operation, job.claims.get("requested_by"),
                result["outcome"])
    if send_all_output(job):
        send_result(job.registration, job.command_id, result)


def send_output(job: Job, final: bool = False) -> bool:
    secrets = secret_values(job.known_values)
    longest = max((len(secret) for secret in secrets), default=0)
    wanted = OUTPUT_LIMIT + longest
    try:
        with open(copies.long_path(output_path(job.command_id)), "rb") as out:
            out.seek(job.offset)
            data = out.read(wanted)
    except OSError:
        return False
    if not data:
        return False
    text, used = next_chunk(data, secrets, final and len(data) < wanted)
    if used == 0:
        return False
    try:
        report(job.registration, job.command_id, fact="output", seq=job.seq, text=text)
    except autoidm.AutoidmError as e:
        job.output_waits = sent_again_later(e)
        return False
    job.output_waits = False
    job.offset += used
    job.seq += 1
    journal_update(job.command_id, output_seq=job.seq, output_offset=job.offset)
    return True


def send_all_output(job: Job) -> bool:
    while send_output(job, final=True):
        pass
    return not job.output_waits


def masked_log(job: Job) -> bytes:
    secrets = secret_values(job.known_values)
    return mask(Path(copies.long_path(output_path(job.command_id))).read_bytes(), secrets)


def log_tail(job: Job) -> str:
    try:
        return masked_log(job)[-MAX_MESSAGE:].decode("utf-8", "replace").strip()
    except OSError:
        return ""


def upload_log(job: Job) -> tuple[str | None, str | None]:
    try:
        data = masked_log(job)
    except OSError as e:
        return None, f"pdt could not read the output: {e}"
    if not data:
        return None, None
    if len(data) > managed_services.MAX_UPLOAD_BYTES:
        return None, (f"The output is {len(data)} bytes, over the limit of "
                      f"{managed_services.MAX_UPLOAD_BYTES} bytes.")
    try:
        return autoidm.upload(job.registration, {
            "kind": "log", "project": job.args["project"], "app": job.args["app"],
            "run_id": job.command_id, "filename": f"{job.command_id}.log"}, data), None
    except autoidm.AutoidmError as e:
        return None, f"AutoIDM did not get the log: {e}"


def artifact_key(job: Job, output: Path) -> tuple[str | None, str | None]:
    if output.is_symlink() or output.is_junction() or not output.is_dir():
        return None, None
    root = output.resolve()
    files = [path for path in sorted(output.rglob("*"))
             if path.is_file() and path.resolve().is_relative_to(root)]
    if not files:
        return None, None
    with tempfile.TemporaryDirectory(dir=work_root()) as folder:
        archive = Path(folder) / ARTIFACT_NAME
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED, strict_timestamps=False) as zipped:
            for path in files:
                zipped.write(path, path.relative_to(output).as_posix())
        size = archive.stat().st_size
        if size > managed_services.MAX_UPLOAD_BYTES:
            return None, (f"The zip of the output folder is {size} bytes, over the limit of "
                          f"{managed_services.MAX_UPLOAD_BYTES} bytes.")
        try:
            return autoidm.upload(job.registration, {
                "kind": "artifact", "project": job.args["project"], "app": job.args["app"],
                "run_id": job.command_id, "filename": ARTIFACT_NAME}, archive), None
        except autoidm.AutoidmError as e:
            return None, f"AutoIDM did not get the artifact: {e}"


def work_root() -> Path:
    folder = service_folder() / "work"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def version_parts(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in re.findall(r"\d+", version)[:3])


def mr_copy_problem(entry: dict, project: Path, app: str, sha: str) -> str:
    """Why a `run_mr` must not run this copy: it is not of the signed commit, or the app pins a
    pdt-cli that writes the run's state over the state of the deployed job."""
    if entry.get("commit") != sha:
        return (f"The copy {entry.get('unit')} is of commit {entry.get('commit') or 'unknown'}, "
                f"and the command names commit {sha}. Run the merge request pipeline again.")
    run_py = project / app / "run.py"
    try:
        text = run_py.read_text(encoding="utf-8-sig")
    except (FileNotFoundError, UnicodeError):
        return ""
    block = SCRIPT_BLOCK.search(text)
    if block is None:
        return ""
    try:
        metadata = tomllib.loads("".join(line[2:] for line in block.group(1).splitlines(True)))
    except tomllib.TOMLDecodeError:
        return ""
    least = version_parts(MR_STATE_VERSION)
    old = None
    for dependency in metadata.get("dependencies") or []:
        found = PDT_REQUIREMENT.fullmatch(dependency) if isinstance(dependency, str) else None
        for operator, version in UPPER_BOUND.findall(((found and found.group(1)) or "").split(";")[0]):
            if version_parts(version) < least or (operator == "<" and version_parts(version) == least):
                old = (dependency, version)
    if old is None:
        return ""
    first = text.count("\n", 0, block.start()) + 1
    rows = block.group(0).splitlines()
    line = first + next((index for index, row in enumerate(rows) if old[1] in row and "pdt" in row.lower()), 0)
    return (f"{app}/run.py line {line} pins pdt-cli {old[1]}. A merge request run needs "
            f"pdt-cli {MR_STATE_VERSION} or newer, which keeps the state of the run apart from "
            "the state of the deployed job.\nNext steps:\n"
            "  pdt upgrade  move pdt, and the pdt version each app pins, to the newest release; "
            "then push the change")


def write_copy_files(job: Job, work: Path) -> Path:
    target = work / "project"
    unit = f"mr={job.args['mr_iid']}"
    found = copies.get(job.registration, job.args["project"], unit)
    if not found:
        raise Refused(f"AutoIDM holds no copy {unit} of {job.args['project']}.")
    copies.fetch(found[0], target)
    pdt_yml = target / config.PROJECT_FILE
    problem = copies.customer_problem(pdt_yml.read_bytes() if pdt_yml.is_file() else None,
                                      job.claims["customer_id"])
    if problem:
        raise copies.CopyError(problem)
    problem = mr_copy_problem(found[0], target, job.args["app"], job.args["sha"])
    if problem:
        raise Refused(problem)
    (target / config.PROJECT_NAME_FILE).write_text(job.args["project"] + "\n", encoding="utf-8")
    return target


def deployed_storage_url(project: Path, app: str) -> dict[str, str]:
    if deployed_platform_of(project, app).get("provider") != "windows":
        return {}
    from pdt import deploy_windows
    with service_commands.in_project(project):
        return {"PDT_STORAGE_URL": deploy_windows.storage_url(app)}


def command_input(job: Job) -> str:
    entry = journal_read(job.command_id) or {}
    try:
        answer = autoidm.post(job.registration, "/v1/commands/input", {
            "version": 1, "command_id": job.command_id, "claim_id": entry.get("claim_id")})
    except autoidm.AutoidmError as e:
        raise Refused(f"AutoIDM gave no value for this command: {e}") from None
    value = answer.get("value")
    if not isinstance(value, str):
        raise Refused("AutoIDM gave no value for this command.")
    return value


def deployed_platform_of(project: Path | None, app: str) -> dict:
    if project is None:
        return {}
    try:
        state = json.loads((project / config.STATE_DIR / "state").read_text(encoding="utf-8"))
        found = state["platforms"][app]
    except (OSError, ValueError, KeyError, TypeError):
        return {}
    return found if isinstance(found, dict) else {}


def stop_deployed(job: Job) -> str:
    app = job.args["app"]
    platform = deployed_platform_of(job.project, app)
    place = managed_services.place(platform)
    if place:
        return f"The job continues in {place}."
    if platform.get("provider") != "windows":
        return "The job continues where it is deployed."
    from pdt import service_backends
    name = f"pdt-{app}"
    try:
        service_backends.run("powershell", "-NoProfile", "-NonInteractive", "-Command",
                             f"Stop-ScheduledTask -TaskName '{name}' -ErrorAction Stop")
    except service_backends.BackendError as e:
        return f"The job continues on this computer, because pdt could not stop the task {name}: {e}"
    return f"pdt stopped the Windows task {name}."


@dataclass
class Prepared:
    cwd: Path
    env: dict[str, str]
    argv: list[str]
    output: Path | None = None
    work: Path | None = None
    stdin: bytes | None = None


def output_folder(app_dir: Path, command_id: str) -> Path:
    return app_dir / ".pdt" / "runs" / command_id / "output"


def prepare(job: Job, base: dict[str, str]) -> Prepared:
    operation = OPERATIONS[job.operation]
    app = job.args.get("app")
    argv = service_commands.command_line(job.operation, job.args)
    if operation.from_copy:
        work = work_root() / job.command_id
        work.mkdir()
        project = write_copy_files(job, work)
        values = env_file_values(job.project, app)
        extra = {"PDT_RUN_ID": job.command_id, "PDT_TRIGGER": "mr",
                 "PDT_MR_IID": job.args["mr_iid"],
                 config.SERVICE_ENV_FILES_ENV: os.pathsep.join(
                     str(path) for path in env_files(job.project, app)) if values else "",
                 **deployed_storage_url(job.project, app)}
        if "app_args" in job.args:
            extra["PDT_APP_ARGS"] = job.args["app_args"]
        output = output_folder(project / app, job.command_id)
        output.mkdir(parents=True, exist_ok=True)
        extra["PDT_OUTPUT_DIR"] = str(output)
        return Prepared(project, child_env({**values, **base}, project, extra), argv,
                        output, work)
    if job.operation == "sync_down":
        target = job.project or Path(base[PROJECTS_HOME_ENV]) / job.args["project"]
        target.mkdir(parents=True, exist_ok=True)
        env = child_env(base, None, {SERVICE_PROJECT_ENV: job.args["project"],
                                         SERVICE_CUSTOMER_ENV: job.claims["customer_id"]})
        env.pop("PDT_PROJECT", None)
        return Prepared(target, env, argv)
    if job.operation == "run" and not job.args["deployed"]:
        app_dir = job.project / app
        extra = {"PDT_RUN_ID": job.command_id}
        if not config.find_env_files(app_dir):
            extra[config.SERVICE_ENV_FILES_ENV] = ""
        output = output_folder(app_dir, job.command_id)
        output.mkdir(parents=True, exist_ok=True)
        extra["PDT_OUTPUT_DIR"] = str(output)
        return Prepared(job.project, child_env(base, job.project, extra), argv, output)
    extra = {}
    if not config.find_env_files(job.project / app if app else job.project):
        extra[config.SERVICE_ENV_FILES_ENV] = ""
    env = child_env(base, job.project, extra)
    if job.operation == "artifacts":
        work = work_root() / job.command_id
        output = work / "output"
        output.mkdir(parents=True)
        return Prepared(job.project, env, [*argv, str(output)], output, work)
    if job.operation == "secrets_set":
        if job.args["name"] not in service_commands.env_spec_names(job.project, app):
            raise Refused(f"{job.args['name']} is not in the env spec of {app}.")
        value = command_input(job)
        job.known_values.append(value)
        return Prepared(job.project, env, argv, stdin=value.encode())
    return Prepared(job.project, env, argv)


def known_values_of(project: Path | None, app: str | None) -> list[str]:
    values = list(env_file_values(project, app).values())
    from pdt import service_install
    password = service_install.proxy_password()
    if password:
        values.append(password)
    return values


def remove_folder(folder: str | Path | None) -> None:
    if folder is not None:
        shutil.rmtree(copies.long_path(Path(folder)), ignore_errors=True)


def execute(job: Job) -> None:
    prepared = None
    try:
        if service_commands.expired(job.claims, job.clock.now()):
            raise Refused("The token expired before the command could start.")
        job.known_values.extend(known_values_of(job.project, job.args.get("app")))
        prepared = prepare(job, dict(os.environ))
        run_child(job, prepared)
    except Refused as e:
        finish(job, result_of("refused", message=str(e)))
    except (copies.CopyError, autoidm.AutoidmError, OSError, RuntimeError, ValueError) as e:
        finish(job, result_of("failed", message=str(e)))
    finally:
        if prepared is not None:
            remove_folder(prepared.work)
        else:
            remove_folder(work_root() / job.command_id)


def start_child(job: Job, prepared: Prepared) -> tuple[subprocess.Popen, int | None]:
    path = output_path(job.command_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(copies.long_path(path), "ab") as out:
        options = dict(tree_options())
        if os.name == "nt":
            options["creationflags"] |= CREATE_SUSPENDED
        proc = subprocess.Popen(prepared.argv, cwd=prepared.cwd, env=prepared.env,
                                stdin=subprocess.DEVNULL if prepared.stdin is None else subprocess.PIPE,
                                stdout=out, stderr=subprocess.STDOUT, **options)
    handle = None
    if os.name == "nt":
        import ctypes
        try:
            handle = run_windows_task.new_job()
            run_windows_task.assign(handle, proc._handle)
        except OSError:
            proc.kill()
            proc.wait()
            raise
        ctypes.WinDLL("ntdll").NtResumeProcess(ctypes.c_void_p(int(proc._handle)))
    if prepared.stdin is not None:
        proc.stdin.write(prepared.stdin)
        proc.stdin.close()
    return proc, handle


def wait_for(job: Job, running, stop) -> tuple[str | None, str | None]:
    deadline = time.monotonic() + job.claims["limit_seconds"]
    next_output = time.monotonic() + OUTPUT_SECONDS
    while running():
        outcome, message = None, None
        if job.cancel.is_set():
            outcome, message = "cancelled", "A person cancelled the command."
        elif job.interrupt.is_set():
            outcome, message = "interrupted", "The pdt service stopped."
        elif time.monotonic() >= deadline:
            outcome, message = "failed", (f"The command passed its run limit of "
                                          f"{job.claims['limit_seconds']} seconds.")
        if outcome is not None:
            stop()
            return outcome, message
        if time.monotonic() >= next_output:
            send_output(job)
            next_output = time.monotonic() + OUTPUT_SECONDS
        time.sleep(0.2)
    return None, None


def run_child(job: Job, prepared: Prepared) -> None:
    proc, handle = start_child(job, prepared)
    created = process_created(proc.pid)
    journal_update(job.command_id, state="started", pid=proc.pid, process_created=created,
                   started_at=now_text())
    write_event(job.command_id, "started", job.operation, job.claims.get("requested_by"), None)
    try:
        report(job.registration, job.command_id, fact="started")
    except autoidm.AutoidmError:
        pass

    def stop() -> None:
        if handle is not None:
            run_windows_task.end_job(handle)
        else:
            stop_tree(proc.pid, lambda: proc.poll() is not None)
        proc.wait()

    outcome, message = wait_for(job, lambda: proc.poll() is None, stop)
    if handle is not None:
        run_windows_task.kernel32().CloseHandle(handle)
    send_all_output(job)
    code = proc.returncode
    if outcome == "cancelled" and job.operation == "run" and job.args.get("deployed"):
        message = stop_deployed(job)
    if outcome is None:
        if code == config.LOCKED_EXIT:
            outcome, message = "refused", log_tail(job)
        elif code == 0:
            outcome = "done"
        else:
            outcome, message = "failed", log_tail(job)
    finish(job, final_result(job, outcome, code, message, prepared))


def final_result(job: Job, outcome: str, code: int | None, message: str | None,
                 prepared: Prepared) -> dict:
    log_key, artifact = None, None
    if job.operation in ("run", "run_mr", "artifacts") and outcome != "refused":
        log_key, problem = upload_log(job)
        message = message or problem
    if (job.operation in ("run", "run_mr") and prepared.output is not None
            and outcome in ("done", "failed")):
        artifact, problem = artifact_key(job, prepared.output)
        message = message or problem
    if job.operation == "artifacts" and outcome == "done":
        artifact, problem = artifact_key(job, prepared.output)
        message = message or problem
    run_id = job.command_id if "PDT_RUN_ID" in prepared.env else None
    return result_of(outcome, code, message, run_id, artifact, log_key)


def follow_adopted(job: Job) -> None:
    pid, entry = job.adopted_pid, journal_read(job.command_id)
    created = entry.get("process_created")

    def stop() -> None:
        stop_tree(pid, lambda: not is_same_process(pid, created))

    outcome, message = wait_for(job, lambda: is_same_process(pid, created), stop)
    send_all_output(job)
    if outcome is None:
        outcome, message = "interrupted", ("The command ended after a restart of the pdt service, "
                                           "so pdt has no exit code for it.")
    finish(job, result_of(outcome, None, message))


class Pool:

    def __init__(self) -> None:
        self.waiting: list[Job] = []
        self.running: dict[str, Job] = {}

    def ids(self) -> list[str]:
        return [*sorted(self.running), *(job.command_id for job in self.waiting)]

    def add(self, job: Job) -> None:
        self.waiting.append(job)

    def start_ready(self) -> None:
        self.reap()
        for job in list(self.waiting):
            limit = MAX_RUNNING + 1 if job.operation == "kill" else MAX_RUNNING
            if len(self.running) >= limit:
                continue
            if any(job.conflicts_with(other) for other in self.running.values()):
                continue
            self.waiting.remove(job)
            self.running[job.command_id] = job
            job.thread = threading.Thread(target=self.start_one, args=(job,), daemon=True)
            job.thread.start()

    def start_one(self, job: Job) -> None:
        execute(job)

    def adopt(self, job: Job) -> None:
        self.running[job.command_id] = job
        job.thread = threading.Thread(target=follow_adopted, args=(job,), daemon=True)
        job.thread.start()

    def reap(self) -> None:
        for command_id, job in list(self.running.items()):
            if not job.thread.is_alive():
                del self.running[command_id]

    def cancel(self, command_id: str) -> bool:
        for job in list(self.waiting):
            if job.command_id == command_id:
                self.waiting.remove(job)
                finish(job, result_of("cancelled",
                                      message="A person cancelled the command before it started."))
                return True
        if command_id in self.running:
            self.running[command_id].cancel.set()
            return True
        return False

    def stop_all(self) -> None:
        for job in self.waiting:
            finish(job, result_of("interrupted",
                                  message="The pdt service stopped before the command started."))
        self.waiting.clear()
        for job in self.running.values():
            job.interrupt.set()
        deadline = time.monotonic() + STOP_SECONDS + 5
        for job in self.running.values():
            job.thread.join(max(0.0, deadline - time.monotonic()))


def job_of_entry(entry: dict, registration: autoidm.Registration, clock: Clock,
                 project_of) -> Job | None:
    try:
        claims = jwt.decode(entry["token"], options={"verify_signature": False})
        operation = OPERATIONS[claims["operation"]]
        project = project_of(claims["args"]["project"])
    except (KeyError, TypeError, jwt.InvalidTokenError):
        return None
    claims["limit_seconds"] = min(claims.get("limit_seconds") or 0, operation.limit_seconds)
    app = claims["args"].get("app")
    job = Job(entry["command_id"], claims, registration, project, clock,
              offset=entry.get("output_offset") or 0, seq=entry.get("output_seq") or 0,
              adopted_pid=entry.get("pid"))
    job.known_values = known_values_of(project, app)
    return job


def recover(pool: Pool, registration: autoidm.Registration, clock: Clock, project_of) -> None:
    held = {*(job.command_id for job in pool.waiting), *pool.running}
    now = time.time()
    for entry in journal_entries():
        command_id = entry["command_id"]
        if command_id in held or entry.get("state") == "claiming":
            continue
        if entry.get("result"):
            if (not entry.get("reported")
                    and send_late_output(entry, registration, clock, project_of)):
                send_result(registration, command_id, entry["result"])
            remove_old_output(command_id, entry, now)
            continue
        if is_same_process(entry.get("pid"), entry.get("process_created")):
            job = job_of_entry(entry, registration, clock, project_of)
            if job is not None:
                pool.adopt(job)
                continue
        result = result_of("interrupted",
                           message="The pdt service stopped while the command ran. It may have run.")
        journal_update(command_id, state="finished", finished_at=now_text(), result=result,
                       reported=False)
        write_event(command_id, "result", entry.get("operation") or "-",
                    entry.get("requested_by"), "interrupted")
        if send_late_output(entry, registration, clock, project_of):
            send_result(registration, command_id, result)
        remove_folder(work_root() / command_id)


def send_late_output(entry: dict, registration: autoidm.Registration, clock: Clock,
                     project_of) -> bool:
    job = job_of_entry(entry, registration, clock, project_of)
    return job is None or send_all_output(job)


def remove_old_output(command_id: str, entry: dict, now: float) -> None:
    path = output_path(command_id)
    try:
        finished = datetime.strptime(entry.get("finished_at") or "", TIME_FORMAT).replace(
            tzinfo=timezone.utc).timestamp()
    except ValueError:
        return
    if now - finished > OUTPUT_KEEP_SECONDS:
        Path(copies.long_path(path)).unlink(missing_ok=True)

