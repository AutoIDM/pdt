import os
import signal
import subprocess
import sys
import threading
import time

import pytest

from pdt import deploy

PROVIDER = """# /// script
# dependencies = []
# ///
import sys
import time

for _ in range(200):
    with open(sys.argv[1], "a") as beats:
        beats.write(".")
    time.sleep(0.05)
"""


def test_ctrl_c_ends_the_provider_script_that_uv_started(monkeypatch, tmp_path):
    script, beats = tmp_path / "provider.py", tmp_path / "beats"
    script.write_text(PROVIDER)
    monkeypatch.setattr(deploy, "provider_command",
                        lambda *args: ["uv", "run", "--script", str(script), str(beats)])
    monkeypatch.setattr(deploy, "provider_env", lambda *args: dict(os.environ))
    monkeypatch.setattr(deploy, "STOP_SECONDS", 0.2)
    interrupted, communicate = [], subprocess.Popen.communicate

    def ctrl_c_once(self, *args, **kwargs):
        if interrupted:
            return communicate(self, *args, **kwargs)
        interrupted.append(True)
        deadline = time.monotonic() + 60
        while not beats.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        raise KeyboardInterrupt

    monkeypatch.setattr(subprocess.Popen, "communicate", ctrl_c_once)
    with pytest.raises(KeyboardInterrupt):
        deploy.dispatch("azure", "deploy", "report", False)
    time.sleep(0.3)
    stopped = beats.read_text()
    time.sleep(0.5)
    assert beats.read_text() == stopped


WRITER = """
import os
import signal
import sys

def stop(*args):
    os.write(1, b"x" * 1_000_000)
    os._exit(7)

signal.signal(signal.SIGINT, signal.SIG_IGN)
signal.signal(signal.SIGTERM, {on_sigterm})
while True:
    sys.stdout.write("x" * 65536)
    sys.stdout.flush()
"""


@pytest.mark.skipif(os.name == "nt", reason="sends a POSIX signal")
@pytest.mark.parametrize("on_sigterm, returncode", [("stop", 7),
                                                    ("signal.SIG_IGN", -signal.SIGKILL)])
def test_ctrl_c_ends_a_provider_script_that_fills_its_output_pipe(
        monkeypatch, tmp_path, on_sigterm, returncode):
    script = tmp_path / "provider.py"
    script.write_text(WRITER.format(on_sigterm=on_sigterm))
    monkeypatch.setattr(deploy, "provider_command",
                        lambda *args: [sys.executable, str(script)])
    monkeypatch.setattr(deploy, "provider_env", lambda *args: dict(os.environ))
    monkeypatch.setattr(deploy, "STOP_SECONDS", 0.5)
    procs, communicate = [], subprocess.Popen.communicate

    def ctrl_c_once(self, *args, **kwargs):
        if not procs:
            procs.append(self)
            time.sleep(0.5)
            raise KeyboardInterrupt
        return communicate(self, *args, **kwargs)

    monkeypatch.setattr(subprocess.Popen, "communicate", ctrl_c_once)
    raised = []

    def stop_the_provider():
        try:
            deploy.dispatch_output("azure", "deploy", "report", [])
        except KeyboardInterrupt:
            raised.append(True)

    worker = threading.Thread(target=stop_the_provider, daemon=True)
    worker.start()
    worker.join(10)
    try:
        assert not worker.is_alive()
    finally:
        procs[0].kill()
    assert (raised, procs[0].returncode) == ([True], returncode)


INTERRUPTED_SCRIPT = """
import time
from concurrent.futures import ThreadPoolExecutor
from pdt.deploy_common import run_main

def main():
    ThreadPoolExecutor(1).submit(time.sleep, 30)
    raise KeyboardInterrupt

run_main(main)
"""


def test_ctrl_c_ends_a_provider_script_at_once_with_no_traceback():
    proc = subprocess.run([sys.executable, "-c", INTERRUPTED_SCRIPT], capture_output=True,
                          text=True, timeout=15)
    assert (proc.returncode, proc.stderr) == (130, "")


FLUSHED_SCRIPT = """
import signal
import sys
import time
from pdt.deploy_common import run_main

class Stdout:
    def write(self, text):
        pass

    def flush(self):
        {flush}
        time.sleep(0.1)

def main():
    sys.stdout = Stdout()
    raise KeyboardInterrupt

run_main(main)
"""


@pytest.mark.parametrize("flush", ["signal.raise_signal(signal.SIGINT)", "raise BrokenPipeError"])
def test_a_second_ctrl_c_or_a_flush_error_still_exits_130(flush):
    proc = subprocess.run([sys.executable, "-c", FLUSHED_SCRIPT.format(flush=flush)],
                          capture_output=True, text=True, timeout=15)
    assert (proc.returncode, proc.stderr) == (130, "")
