#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Run an app once for its Windows scheduled task and keep the output.

usage: uv run --script run_windows_task.py <app dir> <logs folder> <storage url>

Task Scheduler discards stdout and stderr, so the task `pdt-<app>` runs this
script instead of run.py. It first puts itself in a new Windows job object that ends each
process in it when the last handle to it closes (`new_job`), so `Stop-ScheduledTask`, a crash of
this script, or the end of the task stops the app and every process the app started, and the
app's lock is free only when they all ended. It holds the app's lock (`.pdt/locks/app-<app>.lock` of the project)
for the run, and when another process holds that lock it writes the fact to the log and exits 1. It sets PDT_STORAGE_URL and PDT_RUN_ID (the UTC start,
so the app's `runs/` folder is named after the run `pdt runs` lists), runs
`uv run --script run.py` (`script_command`) in the app folder with the uv that started it (or, for
an app with no run.py, `uv run --script run_powershell.py <app dir>`) inside
`lifecycle.py`, which adds the job's lifecycle events to the log (with
PDT_TRIGGER=manual when it finds MANUAL_START), writes
both streams to
<logs folder>\\<UTC start>.log, each line after the UTC time it arrived
(LINE_TIME), ends the file with `pdt: exit N` (the marker `pdt runs` reads),
and exits with the child's exit code so `Get-ScheduledTaskInfo` shows it.
It then writes the result to the Windows Application log under the source
`pdt` (event RUN_SUCCEEDED or RUN_FAILED).

It needs nothing beyond the standard library and never prints: nobody watches
a scheduled task. When uv cannot be started the log holds the reason and
`pdt: exit 127`.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

USAGE = "usage: run_windows_task.py <app dir> <logs folder> <storage url>"
EXIT_MARKER = "pdt: exit "
MISSING_UV = 127
# `pdt run APP --deployed` writes this file in the logs folder before it starts the task,
# since Task Scheduler passes nothing to one run. A run that finds it, at most
# MANUAL_START_SECONDS old, is manual.
MANUAL_START = "manual-start"
MANUAL_START_SECONDS = 300
LINE_TIME = re.compile(r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z) ")
DRAIN_SECONDS = 2
EVENT_SOURCE = "pdt"
RUN_SUCCEEDED = 1000
RUN_FAILED = 1001
EVENTLOG_ERROR_TYPE = 1
EVENTLOG_INFORMATION_TYPE = 4
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9


def timed(raw: bytes) -> bytes:
    moment = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
    return f"{moment.replace('+00:00', 'Z')} ".encode() + raw.rstrip(b"\r\n") + b"\n"


def report(app: str, log: Path, code: int) -> None:
    message = f"pdt app {app}, run {log.stem}, ended with exit code {code}. Log: {log}"
    try:
        import ctypes

        advapi = ctypes.WinDLL("advapi32")
        advapi.RegisterEventSourceW.restype = ctypes.c_void_p
        advapi.ReportEventW.argtypes = (
            ctypes.c_void_p, ctypes.c_ushort, ctypes.c_ushort, ctypes.c_ulong, ctypes.c_void_p,
            ctypes.c_ushort, ctypes.c_ulong, ctypes.POINTER(ctypes.c_wchar_p), ctypes.c_void_p)
        advapi.DeregisterEventSource.argtypes = (ctypes.c_void_p,)
        source = advapi.RegisterEventSourceW(None, EVENT_SOURCE)
        if not source:
            return
        kind, event = ((EVENTLOG_INFORMATION_TYPE, RUN_SUCCEEDED) if code == 0
                       else (EVENTLOG_ERROR_TYPE, RUN_FAILED))
        advapi.ReportEventW(source, kind, 0, event, None, 1, 0,
                            (ctypes.c_wchar_p * 1)(message), None)
        advapi.DeregisterEventSource(source)
    except (AttributeError, OSError):
        pass


def kernel32():
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateJobObjectW.restype = wintypes.HANDLE
    kernel.CreateJobObjectW.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR)
    kernel.SetInformationJobObject.argtypes = (wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                               wintypes.DWORD)
    kernel.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
    kernel.TerminateJobObject.argtypes = (wintypes.HANDLE, wintypes.UINT)
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    return kernel


def new_job() -> int:
    import ctypes

    class Basic(ctypes.Structure):
        _fields_ = [("per_process_user_time_limit", ctypes.c_int64),
                    ("per_job_user_time_limit", ctypes.c_int64),
                    ("limit_flags", ctypes.c_uint32),
                    ("minimum_working_set_size", ctypes.c_size_t),
                    ("maximum_working_set_size", ctypes.c_size_t),
                    ("active_process_limit", ctypes.c_uint32),
                    ("affinity", ctypes.c_size_t),
                    ("priority_class", ctypes.c_uint32),
                    ("scheduling_class", ctypes.c_uint32)]

    class Extended(ctypes.Structure):
        _fields_ = [("basic", Basic), ("io", ctypes.c_uint64 * 6),
                    ("process_memory_limit", ctypes.c_size_t),
                    ("job_memory_limit", ctypes.c_size_t),
                    ("peak_process_memory_used", ctypes.c_size_t),
                    ("peak_job_memory_used", ctypes.c_size_t)]

    kernel = kernel32()
    job = kernel.CreateJobObjectW(None, None)
    if not job:
        raise ctypes.WinError(ctypes.get_last_error())
    info = Extended()
    info.basic.limit_flags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not kernel.SetInformationJobObject(job, JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
                                          ctypes.byref(info), ctypes.sizeof(info)):
        error = ctypes.get_last_error()
        kernel.CloseHandle(job)
        raise ctypes.WinError(error)
    return job


def assign(job: int, process: int) -> None:
    import ctypes
    if not kernel32().AssignProcessToJobObject(job, process):
        raise ctypes.WinError(ctypes.get_last_error())


def end_job(job: int) -> None:
    kernel32().TerminateJobObject(job, 1)


def hold_in_job() -> int | None:
    if os.name != "nt":
        return None
    job = new_job()
    assign(job, kernel32().GetCurrentProcess())
    return job


def script_command(uv: str, folder: Path, script: str = "run.py") -> list[str]:
    """`uv run` of the app's script in `folder`. uv runs a script with inline metadata in an
    environment of its own. A script with none would run in a `.venv` that uv finds in a parent
    folder, so it gets `--isolated`, which uv reports as a no-op for a script with metadata."""
    try:
        text = (folder / script).read_text(encoding="utf-8", errors="replace")
    except OSError:
        text = ""
    isolated = [] if "# /// script" in text else ["--isolated"]
    return [uv, "run", *isolated, "--script", script]


def take_lock(path: Path, project: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path, "w")
    try:
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        raise RuntimeError(f"Another process holds the lock {path} of the project {project.name}.") from None
    return handle


def app_lock(app_dir: Path):
    project = next((folder for folder in app_dir.parents if (folder / "pdt.yml").is_file()),
                   app_dir.parent)
    locks = project / ".pdt" / "locks"
    whole = take_lock(locks / "project.lock", project)
    try:
        return take_lock(locks / f"app-{app_dir.name}.lock", project)
    finally:
        whole.close()


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        sys.stderr.write(USAGE + "\n")
        return 2
    app_dir, logs, storage_url = argv
    folder = Path(logs)
    folder.mkdir(parents=True, exist_ok=True)
    stamp = f"{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}"
    log = folder / f"{stamp}.log"
    env = {**os.environ, "PDT_STORAGE_URL": storage_url, "PDT_RUN_ID": stamp}
    marker = folder / MANUAL_START
    if marker.is_file():
        if time.time() - marker.stat().st_mtime < MANUAL_START_SECONDS:
            env["PDT_TRIGGER"] = "manual"
        marker.unlink(missing_ok=True)
    env.setdefault("PYTHONIOENCODING", "utf-8")
    # Python holds stdout written to a file or pipe until a buffer fills, so a print()
    # line would land after a later stderr line. The cloud images set the same.
    env.setdefault("PYTHONUNBUFFERED", "1")
    uv = os.environ.get("UV") or "uv"
    if (Path(app_dir) / "run.py").is_file():
        command = script_command(uv, Path(app_dir))
    else:
        command = [uv, "run", "--script", str(Path(__file__).with_name("run_powershell.py")), app_dir]
    command = [uv, "run", "--script", str(Path(__file__).with_name("lifecycle.py")), "--", *command]
    with log.open("wb") as out:
        try:
            job = hold_in_job()
        except OSError as exc:
            out.write(timed(f"pdt could not make the job object of this run: {exc}".encode()))
            out.write(timed(f"{EXIT_MARKER}1".encode()))
            report(Path(app_dir).name, log, 1)
            return 1
        try:
            held = app_lock(Path(app_dir))
        except (OSError, RuntimeError) as exc:
            out.write(timed(str(exc).encode()))
            out.write(timed(f"{EXIT_MARKER}1".encode()))
            report(Path(app_dir).name, log, 1)
            return 1
        try:
            child = subprocess.Popen(
                command, cwd=app_dir, env=env,
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        except OSError as exc:
            out.write(timed(f"{uv} could not be started: {exc}".encode()))
            code = MISSING_UV
        else:
            lock = threading.Lock()
            ended = []
            rest = [b""]

            def copy() -> None:
                while True:
                    chunk = child.stdout.read1()
                    with lock:
                        if ended:
                            return
                        if chunk == b"":
                            chunk = b"\n" if rest[0] else b""
                            ended.append(True)
                        *complete, rest[0] = (rest[0] + chunk).split(b"\n")
                        for raw in complete:
                            out.write(timed(raw))
                        out.flush()
            reader = threading.Thread(target=copy, daemon=True)
            reader.start()
            try:
                code = child.wait()
            except BaseException:
                child.kill()
                child.wait()
                raise
            # A process the app started in the background can hold the pipe open after the
            # app exits, so the run ends without waiting for it.
            reader.join(DRAIN_SECONDS)
            with lock:
                ended.append(True)
                if rest[0]:
                    out.write(timed(rest[0]))
        held.close()
        out.write(timed(f"{EXIT_MARKER}{code}".encode()))
    report(Path(app_dir).name, log, code)
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
