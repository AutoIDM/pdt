"""`python -m pdt.service_main`: the program of the pdt service, which each service manager starts
(pdt.service_backends). A person starts it with `pdt service start`.

On Windows the Service Control Manager starts it. `StartServiceCtrlDispatcher` connects the process
to the SCM on a thread of its own, and the loop runs on the main thread, where the console prints.
A stop or a shutdown from the SCM ends the loop as SIGTERM does elsewhere. The console lines go to
`windows.log` in the service folder, as they go to `launchd.log` on macOS. From a terminal, the
dispatcher fails with ERROR_FAILED_SERVICE_CONTROLLER_CONNECT, and the loop runs in the foreground.
https://learn.microsoft.com/en-us/windows/win32/api/winsvc/nf-winsvc-startservicectrldispatcherw
"""

from __future__ import annotations

import argparse
import os
import signal
import sys
import threading
from typing import Callable

from pdt import console, service, service_backends, service_runner

SERVICE_WIN32_OWN_PROCESS = 0x10
SERVICE_STOPPED, SERVICE_STOP_PENDING, SERVICE_RUNNING = 1, 3, 4
SERVICE_ACCEPT_STOP, SERVICE_ACCEPT_SHUTDOWN = 0x1, 0x4
SERVICE_CONTROL_STOP, SERVICE_CONTROL_SHUTDOWN = 1, 5
ERROR_SERVICE_SPECIFIC_ERROR = 1066
STOP_WAIT_MS = (service_runner.STOP_SECONDS + 10) * 1000


def windows_service(run: Callable[[threading.Event], int]) -> int | None:
    import ctypes
    from ctypes import wintypes

    class Status(ctypes.Structure):
        _fields_ = [(name, wintypes.DWORD) for name in (
            "service_type", "current_state", "controls_accepted", "win32_exit_code",
            "service_specific_exit_code", "check_point", "wait_hint")]

    handler_type = ctypes.WINFUNCTYPE(wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
                                      wintypes.LPVOID, wintypes.LPVOID)
    main_type = ctypes.WINFUNCTYPE(None, wintypes.DWORD, ctypes.POINTER(wintypes.LPWSTR))

    class Entry(ctypes.Structure):
        _fields_ = [("name", wintypes.LPWSTR), ("main", main_type)]

    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    advapi.RegisterServiceCtrlHandlerExW.restype = wintypes.HANDLE
    advapi.RegisterServiceCtrlHandlerExW.argtypes = [wintypes.LPCWSTR, handler_type,
                                                     wintypes.LPVOID]
    advapi.SetServiceStatus.argtypes = [wintypes.HANDLE, ctypes.POINTER(Status)]
    stop, started = threading.Event(), threading.Event()
    handle = wintypes.HANDLE()

    def report(state: int, code: int = 0, wait_ms: int = 0) -> None:
        accepted = SERVICE_ACCEPT_STOP | SERVICE_ACCEPT_SHUTDOWN if state == SERVICE_RUNNING else 0
        status = Status(SERVICE_WIN32_OWN_PROCESS, state, accepted,
                        ERROR_SERVICE_SPECIFIC_ERROR if code else 0, code, 0, wait_ms)
        advapi.SetServiceStatus(handle, ctypes.byref(status))

    @handler_type
    def handler(control: int, _event: int, _data: object, _context: object) -> int:
        if control in (SERVICE_CONTROL_STOP, SERVICE_CONTROL_SHUTDOWN):
            report(SERVICE_STOP_PENDING, wait_ms=STOP_WAIT_MS)
            stop.set()
        return 0

    @main_type
    def service_main(_argc: int, _argv: object) -> None:
        handle.value = advapi.RegisterServiceCtrlHandlerExW(service_backends.NAME, handler, None)
        report(SERVICE_RUNNING)
        started.set()

    table = (Entry * 2)(Entry(service_backends.NAME, service_main), Entry(None, main_type()))
    dispatcher = threading.Thread(target=advapi.StartServiceCtrlDispatcherW, args=(table,),
                                  daemon=True)
    dispatcher.start()
    while not started.wait(0.1):
        if not dispatcher.is_alive():
            return None
    output = service_runner.service_folder() / "windows.log"
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.is_file() and output.stat().st_size > service.LOG_LIMIT_BYTES:
        os.replace(output, output.with_suffix(".log.1"))
    sys.stdout = sys.stderr = open(output, "a", encoding="utf-8", buffering=1)
    console.to_file(sys.stdout)
    code = 1
    try:
        code = run(stop)
    finally:
        console.drain()
        report(SERVICE_STOPPED, code)
        dispatcher.join(10)
    return code


def main(argv: list[str] | None = None) -> int:
    argparse.ArgumentParser(prog="python -m pdt.service_main").parse_args(argv)
    if os.name == "nt":
        code = windows_service(service.run)
        if code is not None:
            return code
    stop = threading.Event()
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, lambda _signum, _frame: stop.set())
    return service.run(stop)


if __name__ == "__main__":
    sys.exit(main())
