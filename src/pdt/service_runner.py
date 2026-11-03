"""How `pdt service` runs one command: its journal entry, its child process, and its reports.

Each command is a child process of the same pdt, started with a list of arguments and no shell,
in a new process group on POSIX and with no window on Windows. Output goes to AutoIDM at most
once each OUTPUT_SECONDS. A cancel, the run limit, or a stop of the service ends the whole
process tree. At most MAX_RUNNING commands run at once, and at most one for each project and
app; the others wait in the Pool.

The journal is one file per command in `<data folder>/service/journal/`. A file in the phase
`claiming` holds a command whose claim got no answer, so a repeat of that ID tries the claim
again. Every other file makes a repeat of its ID a refusal. `recover` reports `interrupted` for
an entry with no result whose process does not run.

A cloud worker's child gets a short allow list of env vars and the credentials of the grant,
and nothing else from the worker's environment. They stay in that environment: pdt writes them
to no file.
"""

from __future__ import annotations

import codecs
import json
import os
import shutil
import signal
import subprocess
import tempfile
import threading
import time
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from dotenv import dotenv_values

from pdt import config, managed_services, service_commands, snapshots
from pdt.deploy_common import KEEP_SECRET_ENV
from pdt.service_commands import OPERATIONS, Refused
from pdt.utils import autoidm

MAX_RUNNING = 4
OUTPUT_SECONDS = 10
STOP_SECONDS = 10
MAX_MESSAGE = 4096
# The contract allows 200 KiB of text, and the body limit counts the JSON form of it.
OUTPUT_LIMIT = 200 * 1024
OUTPUT_KEPT = 4 * OUTPUT_LIMIT
TIME_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
SERVICE_PROJECT_ENV = "PDT_SERVICE_PROJECT"
SERVICE_CUSTOMER_ENV = "PDT_SERVICE_CUSTOMER"
GRANT_FOLDER_ENV = "PDT_GRANT_FOLDER"
PROJECTS_HOME_ENV = "PDT_SERVICE_PROJECTS"
WORKER_ENV_KEEP = ("PATH", "PATHEXT", "HOME", "USERPROFILE", "SYSTEMROOT", "COMSPEC", "TEMP", "TMP",
                   "TMPDIR", "LANG", "LC_ALL", "PROGRAMDATA", "LOCALAPPDATA", "APPDATA",
                   "XDG_DATA_HOME", "XDG_CACHE_HOME", "UV_CACHE_DIR", "UV_PYTHON_INSTALL_DIR",
                   "UV_TOOL_DIR", "SSL_CERT_FILE")
NOT_FOR_A_CHILD = set(autoidm.SECRET_NAMES)


def now_text() -> str:
    return datetime.now(timezone.utc).strftime(TIME_FORMAT)


def service_folder() -> Path:
    return autoidm.folder().parent / "service"


def journal_path(command_id: str) -> Path:
    return service_folder() / "journal" / f"{command_id}.json"


def journal_read(command_id: str) -> dict | None:
    try:
        return json.loads(journal_path(command_id).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def journal_begin(entry: dict) -> None:
    path = journal_path(entry["command_id"])
    path.parent.mkdir(parents=True, exist_ok=True)
    config.write_text_atomically(path, json.dumps(entry) + "\n")


def journal_update(command_id: str, **fields) -> None:
    entry = journal_read(command_id) or {"command_id": command_id}
    config.write_text_atomically(journal_path(command_id),
                                 json.dumps({**entry, **fields}) + "\n")


def journal_entries() -> list[dict]:
    entries = []
    for path in sorted((service_folder() / "journal").glob("cmd_*.json")):
        try:
            entries.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            continue
    return entries


def process_runs(pid: int) -> bool:
    if os.name == "nt":
        # os.kill with signal 0 ends a process on Windows, so it cannot ask this.
        proc = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH", "/FO", "CSV"],
                              stdin=subprocess.DEVNULL, capture_output=True, text=True, check=False)
        return f'"{pid}"' in proc.stdout
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def stop_tree(pid: int, finished) -> None:
    """End the process `pid` and each process it started. POSIX gets a stop request first and
    STOP_SECONDS to act on it. Windows has no such request for a process with no console."""
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                       stdin=subprocess.DEVNULL, capture_output=True, check=False)
        return
    try:
        os.killpg(pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return
    deadline = time.monotonic() + STOP_SECONDS
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


def child_env(base: dict[str, str], project: Path | None, preset: dict[str, str],
              extra: dict[str, str]) -> dict[str, str]:
    env = {name: value for name, value in base.items() if name not in NOT_FOR_A_CHILD}
    env["PYTHONUTF8"] = "1"
    if project is not None:
        env["PDT_PROJECT"] = str(project)
    return {**env, **extra, **preset}


def worker_env(base: dict[str, str], project: Path, credentials: dict[str, str]) -> dict[str, str]:
    """The env of a worker's child. The worker's own cloud login and registration stay out."""
    wanted = {name.upper() for name in WORKER_ENV_KEEP}
    kept = {name: value for name, value in base.items() if name.upper() in wanted}
    # A snapshot holds customer files, so a uv.toml in one must not choose a package index.
    return {**kept, **credentials, "PDT_PROJECT": str(project), "PYTHONUTF8": "1",
            "UV_NO_CONFIG": "1", "CI": "pdt-service", KEEP_SECRET_ENV: "1"}


def env_file_values(project: Path | None, app: str | None) -> dict[str, str]:
    """The values of the served project's .env files, nearest first, as `pdt run` reads them.
    A snapshot holds no .env, so the service passes them to the child in its environment."""
    if project is None:
        return {}
    values: dict[str, str] = {}
    folders = [project / app, project] if app else [project]
    for folder in folders:
        path = folder / ".env"
        if path.is_file():
            for name, value in dotenv_values(path, encoding="utf-8-sig").items():
                values.setdefault(name, value or "")
    return values


@dataclass
class Job:
    command_id: str
    claims: dict
    registration: autoidm.Registration
    project: Path | None
    worker: bool = False
    preset: dict | None = None
    cancel: threading.Event = field(default_factory=threading.Event)
    interrupt: threading.Event = field(default_factory=threading.Event)
    thread: threading.Thread | None = None
    output: bytearray = field(default_factory=bytearray)
    output_lock: threading.Lock = field(default_factory=threading.Lock)
    seq: int = 0

    @property
    def operation(self) -> str:
        return self.claims["operation"]

    @property
    def args(self) -> dict:
        return self.claims["args"]

    @property
    def key(self) -> tuple[str, str | None]:
        return self.args["project"], self.args.get("app")


def report(registration: autoidm.Registration, command_id: str, **facts) -> dict:
    return autoidm.post(registration, "/v1/commands/report",
                        {"version": 1, "command_id": command_id, **facts})


def send_result(registration: autoidm.Registration, command_id: str, result: dict) -> bool:
    """Send a result the journal holds. False when a later try can help: no answer, a 401 of a
    registration that can come back, or a 5xx. AutoIDM keeps the first result, so a repeat is a
    success, and another 4xx will not change."""
    message = result.get("message")
    sent = {**result, "message": message[:MAX_MESSAGE] if message else None}
    try:
        report(registration, command_id, fact="result", **sent)
    except autoidm.AutoidmError as e:
        if e.status == 0 or e.status == 401 or e.status >= 500:
            return False
    journal_update(command_id, reported=True)
    return True


def finish(job: Job, outcome: str, exit_code: int | None = None, message: str | None = None,
           run_id: str | None = None, artifact_key: str | None = None) -> None:
    result = {"outcome": outcome, "exit_code": exit_code, "message": message, "run_id": run_id,
              "artifact_key": artifact_key}
    journal_update(job.command_id, phase="finished", finished_at=now_text(), result=result,
                   reported=False)
    send_result(job.registration, job.command_id, result)


def read_output(job: Job, stream) -> None:
    for line in stream:
        with job.output_lock:
            job.output.extend(line)
            if len(job.output) > OUTPUT_KEPT:
                dropped = len(job.output) - OUTPUT_KEPT // 2
                del job.output[:dropped]
                job.output[:0] = f"[pdt dropped {dropped} bytes of output here]\n".encode()


def next_chunk(data: bytes) -> tuple[str, int]:
    """The longest start of `data`, up to halves of OUTPUT_LIMIT, whose JSON form fits the limit,
    and the bytes it used. It ends at a whole UTF-8 character, unless `data` holds no whole one."""
    size = OUTPUT_LIMIT
    while True:
        decoder = codecs.getincrementaldecoder("utf-8")("replace")
        text = decoder.decode(data[:size])
        used = min(size, len(data)) - len(decoder.getstate()[0])
        if used <= 0:
            return data.decode("utf-8", "replace"), len(data)
        if len(json.dumps(text)) <= OUTPUT_LIMIT:
            return text, used
        size //= 2


def send_output(job: Job) -> None:
    """Send one chunk of the buffer. A failed send changes nothing for the run: the run goes on."""
    with job.output_lock:
        if not job.output:
            return
        text, used = next_chunk(bytes(job.output[:OUTPUT_LIMIT]))
        del job.output[:used]
    job.seq += 1
    try:
        report(job.registration, job.command_id, fact="output", seq=job.seq, text=text)
    except autoidm.AutoidmError:
        pass


def artifact_key(job: Job, app_dir: Path) -> tuple[str | None, str | None]:
    """Zip the run's output folder and upload it. Returns the key, or a message that says why
    no artifact went up."""
    output = app_dir / ".pdt" / "runs" / job.command_id / "output"
    if output.is_symlink() or output.is_junction() or not output.is_dir():
        return None, "The run left no output folder."
    root = output.resolve()
    with tempfile.TemporaryDirectory(dir=work_root()) as folder:
        archive = Path(folder) / f"{job.command_id}.zip"
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED, strict_timestamps=False) as zipped:
            for path in sorted(output.rglob("*")):
                if path.is_file() and path.resolve().is_relative_to(root):
                    zipped.write(path, path.relative_to(output).as_posix())
        if archive.stat().st_size > managed_services.MAX_UPLOAD_BYTES:
            return None, "The output folder's zip is over the 100 MiB upload limit."
        try:
            return autoidm.upload(job.registration, {
                "kind": "artifact", "project": job.args["project"], "app": job.args["app"],
                "run_id": job.command_id, "filename": archive.name},
                archive.read_bytes()), None
        except autoidm.AutoidmError as e:
            return None, f"AutoIDM did not get the artifact: {e}"


def work_root() -> Path:
    folder = service_folder() / "work"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def write_snapshot_files(job: Job, work: Path) -> None:
    """Fetch the snapshot into `work`, and name the project as the portal names it, because the
    folder's own name is a command ID."""
    target = work / "project"
    target.mkdir()
    snapshots.fetch(job.registration, job.args["project"], job.args["snapshot_id"], target,
                    job.claims["customer_id"])
    (target / config.PROJECT_NAME_FILE).write_text(job.args["project"] + "\n", encoding="utf-8")


@dataclass
class Prepared:
    cwd: Path
    env: dict[str, str]
    argv: list[str]
    app_dir: Path | None = None
    work: Path | None = None
    grant_folder: str | None = None


def prepare(job: Job, base: dict[str, str]) -> Prepared:
    """The folder, the env, and the command line of a job."""
    operation = OPERATIONS[job.operation]
    app = job.args.get("app")
    preset = job.preset or {"env": {}, "artifacts": False}
    argv = service_commands.command_line(job.operation, job.args)
    if job.worker:
        work = work_root() / job.command_id
        work.mkdir()
        write_snapshot_files(job, work)
        project = work / "project"
        if app:
            # A record of the deploy lives on the computer that deployed, and a worker has none.
            state = project / config.STATE_DIR / "state"
            state.parent.mkdir(exist_ok=True)
            state.write_text(json.dumps({"deployed": [app]}) + "\n", encoding="utf-8")
        from pdt import grants
        credentials = grants.credentials(job.claims["grant"], job.registration)
        if job.operation == "run":
            # The grant does not read logs, so a worker starts the run and does not follow it.
            argv = [part for part in argv if part != "--follow"]
        return Prepared(project, worker_env(base, project, credentials), argv,
                        work=work, grant_folder=credentials.get(GRANT_FOLDER_ENV))
    if operation.from_snapshot:
        work = work_root() / job.command_id
        work.mkdir()
        write_snapshot_files(job, work)
        project = work / "project"
        found = {**env_file_values(job.project, app), **base}
        extra = {"PDT_RUN_ID": job.command_id}
        if preset["artifacts"]:
            extra["PDT_OUTPUT_DIR"] = str(project / app / ".pdt" / "runs" / job.command_id / "output")
        return Prepared(project, child_env(found, project, preset["env"], extra), argv,
                        project / app, work)
    if job.operation == "sync_down":
        target = job.project or Path(base[PROJECTS_HOME_ENV]) / job.args["project"]
        target.mkdir(parents=True, exist_ok=True)
        env = child_env(base, None, {}, {SERVICE_PROJECT_ENV: job.args["project"],
                                         SERVICE_CUSTOMER_ENV: job.claims["customer_id"]})
        env.pop("PDT_PROJECT", None)
        return Prepared(target, env, argv)
    if job.operation == "run" and not job.args["deployed"]:
        app_dir = job.project / app
        extra = {"PDT_RUN_ID": job.command_id}
        if preset["artifacts"]:
            extra["PDT_OUTPUT_DIR"] = str(app_dir / ".pdt" / "runs" / job.command_id / "output")
        return Prepared(job.project, child_env(base, job.project, preset["env"], extra), argv,
                        app_dir)
    return Prepared(job.project, child_env(base, job.project, preset["env"], {}), argv)


def remove_folder(folder: str | Path | None) -> None:
    if folder is not None:
        shutil.rmtree(snapshots.long_path(Path(folder)), ignore_errors=True)


def execute(job: Job) -> None:
    prepared = None
    try:
        prepared = prepare(job, dict(os.environ))
        run_child(job, prepared)
    except (Refused, snapshots.SnapshotError, autoidm.AutoidmError, OSError,
            RuntimeError, ValueError) as e:
        finish(job, "failed", message=str(e))
    finally:
        if prepared is not None:
            remove_folder(prepared.grant_folder)
            remove_folder(prepared.work)
        else:
            remove_folder(work_root() / job.command_id)


def run_child(job: Job, prepared: Prepared) -> None:
    proc = subprocess.Popen(prepared.argv, cwd=prepared.cwd, env=prepared.env,
                            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, **tree_options())
    journal_update(job.command_id, phase="running", pid=proc.pid, started_at=now_text())
    try:
        report(job.registration, job.command_id, fact="started")
    except autoidm.AutoidmError:
        pass
    reader = threading.Thread(target=read_output, args=(job, proc.stdout), daemon=True)
    reader.start()
    deadline = time.monotonic() + job.claims["limit_seconds"]
    next_output = time.monotonic() + OUTPUT_SECONDS
    outcome, message = None, None
    while proc.poll() is None:
        if job.cancel.is_set():
            outcome, message = "cancelled", "A person cancelled the command."
        elif job.interrupt.is_set():
            outcome, message = "interrupted", "The pdt service stopped."
        elif time.monotonic() >= deadline:
            outcome, message = "failed", (f"The command passed its run limit of "
                                          f"{job.claims['limit_seconds']} seconds.")
        if outcome is not None:
            stop_tree(proc.pid, lambda: proc.poll() is not None)
            proc.wait()
            break
        if time.monotonic() >= next_output:
            send_output(job)
            next_output = time.monotonic() + OUTPUT_SECONDS
        time.sleep(0.2)
    reader.join(timeout=5)
    while job.output:
        send_output(job)
    code = proc.returncode
    key = None
    if outcome is None:
        outcome = "done" if code == 0 else "failed"
    wanted = job.preset and job.preset["artifacts"] and prepared.app_dir is not None
    if wanted and outcome in ("done", "failed"):
        key, problem = artifact_key(job, prepared.app_dir)
        message = message or problem
    run_id = job.command_id if "PDT_RUN_ID" in prepared.env else None
    finish(job, outcome, code, message, run_id, key)


class Pool:
    """The commands this service holds: the ones that wait for a free slot, the ones that run,
    and the ones an earlier service started whose process still runs."""

    def __init__(self) -> None:
        self.waiting: list[Job] = []
        self.running: dict[str, Job] = {}
        self.adopted: dict[str, dict] = {}

    def ids(self) -> list[str]:
        return [*sorted(self.running), *sorted(self.adopted),
                *(job.command_id for job in self.waiting)]

    def add(self, job: Job) -> None:
        self.waiting.append(job)

    def start_ready(self) -> None:
        self.reap()
        for job in list(self.waiting):
            busy = {running.key for running in self.running.values()}
            if len(self.running) >= MAX_RUNNING:
                return
            if job.key in busy:
                continue
            self.waiting.remove(job)
            self.running[job.command_id] = job
            job.thread = threading.Thread(target=execute, args=(job,), daemon=True)
            job.thread.start()

    def reap(self) -> None:
        for command_id, job in list(self.running.items()):
            if not job.thread.is_alive():
                del self.running[command_id]

    def cancel(self, command_id: str) -> None:
        for job in list(self.waiting):
            if job.command_id == command_id:
                self.waiting.remove(job)
                finish(job, "cancelled", message="A person cancelled the command before it started.")
        if command_id in self.running:
            self.running[command_id].cancel.set()
        if command_id in self.adopted:
            pid = self.adopted[command_id]["pid"]
            stop_tree(pid, lambda: not process_runs(pid))

    def stop_all(self) -> None:
        for job in self.waiting:
            finish(job, "interrupted", message="The pdt service stopped before the command started.")
        self.waiting.clear()
        for job in self.running.values():
            job.interrupt.set()
        deadline = time.monotonic() + STOP_SECONDS + 5
        for job in self.running.values():
            job.thread.join(max(0.0, deadline - time.monotonic()))


def recover(pool: Pool, registrations: dict[str, autoidm.Registration]) -> None:
    """Report each journal entry that lost its service, and send each result AutoIDM did not
    get. An entry whose process still runs stays in `pool.adopted` until the process ends."""
    held = {*(job.command_id for job in pool.waiting), *pool.running}
    for entry in journal_entries():
        command_id = entry.get("command_id")
        registration = registrations.get(entry.get("customer_id"))
        if command_id in held or registration is None:
            continue
        if entry.get("result"):
            if not entry.get("reported"):
                send_result(registration, command_id, entry["result"])
            continue
        if entry.get("phase") == "claiming":
            continue
        pid = entry.get("pid")
        if pid and process_runs(pid):
            pool.adopted[command_id] = entry
            continue
        pool.adopted.pop(command_id, None)
        result = {"outcome": "interrupted", "exit_code": None, "run_id": None, "artifact_key": None,
                  "message": "The pdt service stopped while the command ran. It may have run."}
        journal_update(command_id, phase="finished", finished_at=now_text(), result=result,
                       reported=False)
        send_result(registration, command_id, result)
        remove_folder(work_root() / command_id)
