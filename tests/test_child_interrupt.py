import os
import signal
import subprocess
import sys
import time

import pytest

PROVIDER = """
import pathlib, sys, time
pathlib.Path(sys.argv[1], "started").touch()
try:
    time.sleep(30)
except KeyboardInterrupt:
    time.sleep(1)
    pathlib.Path(sys.argv[1], "stopped").touch()
"""

# Like `uv run`, it leaves Ctrl+C to the script and waits for it.
UV = """
import signal, subprocess, sys
signal.signal(signal.SIGINT, lambda *args: None)
sys.exit(subprocess.run([sys.executable, "-c", sys.argv[1], sys.argv[2]]).returncode)
"""

PDT = """
import sys
from pdt import deploy
try:
    deploy.run_child([sys.executable, "-c", sys.argv[1], sys.argv[2], sys.argv[3]])
except KeyboardInterrupt:
    sys.exit(130)
"""


@pytest.mark.skipif(os.name == "nt", reason="Windows cannot send Ctrl+C to one process group: "
                    "CTRL_C_EVENT reaches every process on the console, pytest too")
def test_ctrl_c_returns_only_after_the_provider_script_stops(tmp_path):
    pdt = subprocess.Popen([sys.executable, "-c", PDT, UV, PROVIDER, str(tmp_path)],
                           start_new_session=True)
    deadline = time.monotonic() + 30
    while not (tmp_path / "started").exists():
        assert time.monotonic() < deadline and pdt.poll() is None
        time.sleep(0.05)
    os.killpg(pdt.pid, signal.SIGINT)
    assert pdt.wait(timeout=30) == 130
    assert (tmp_path / "stopped").exists(), "pdt returned while the provider script still ran"
