import os
import signal
import subprocess
import sys
import time

import pytest

PROVIDER = """
import os, pathlib, signal, sys, time
pathlib.Path(sys.argv[1], "started").write_text(str(os.getpid()))
try:
    time.sleep(30)
except KeyboardInterrupt:
    if len(sys.argv) > 2:
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        time.sleep(60)
    time.sleep(1)
    pathlib.Path(sys.argv[1], "stopped").touch()
"""

# Like `uv run`, it leaves Ctrl+C to the script and waits for it.
UV = """
import signal, subprocess, sys
signal.signal(signal.SIGINT, lambda *args: None)
sys.exit(subprocess.run([sys.executable, "-c", *sys.argv[1:]]).returncode)
"""

PDT = """
import sys
from pdt import deploy
try:
    deploy.run_child([sys.executable, "-c", *sys.argv[1:]])
except KeyboardInterrupt:
    sys.exit(130)
"""


NOT_ON_WINDOWS = pytest.mark.skipif(
    os.name == "nt", reason="Windows cannot send Ctrl+C to one process group: "
    "CTRL_C_EVENT reaches every process on the console, pytest too")


def start(tmp_path, *extra):
    pdt = subprocess.Popen([sys.executable, "-c", PDT, UV, PROVIDER, str(tmp_path), *extra],
                           start_new_session=True)
    deadline = time.monotonic() + 30
    while not (tmp_path / "started").exists():
        assert time.monotonic() < deadline and pdt.poll() is None
        time.sleep(0.05)
    time.sleep(0.1)
    return pdt


@NOT_ON_WINDOWS
def test_ctrl_c_returns_only_after_the_provider_script_stops(tmp_path):
    pdt = start(tmp_path)
    os.killpg(pdt.pid, signal.SIGINT)
    assert pdt.wait(timeout=30) == 130
    assert (tmp_path / "stopped").exists(), "pdt returned while the provider script still ran"


@NOT_ON_WINDOWS
def test_second_ctrl_c_stops_the_provider_script_at_once(tmp_path):
    pdt = start(tmp_path, "hang")
    provider = int((tmp_path / "started").read_text())
    os.killpg(pdt.pid, signal.SIGINT)
    time.sleep(1)
    assert pdt.poll() is None, "pdt returned after one Ctrl+C while the provider script still ran"
    started = time.monotonic()
    os.killpg(pdt.pid, signal.SIGINT)
    assert pdt.wait(timeout=10) == 130
    assert time.monotonic() - started < 5
    with pytest.raises(ProcessLookupError):
        os.kill(provider, 0)
