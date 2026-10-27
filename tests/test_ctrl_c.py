import os
import subprocess
import sys
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

    def ctrl_c(self, *args, **kwargs):
        deadline = time.monotonic() + 60
        while not beats.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        raise KeyboardInterrupt

    monkeypatch.setattr(subprocess.Popen, "communicate", ctrl_c)
    with pytest.raises(KeyboardInterrupt):
        deploy.dispatch("azure", "deploy", "report", False)
    time.sleep(0.3)
    stopped = beats.read_text()
    time.sleep(0.5)
    assert beats.read_text() == stopped


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
