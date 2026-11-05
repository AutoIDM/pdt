import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from pdt import run_windows_task

HEADER = '# /// script\n# requires-python = ">=3.12"\n# dependencies = []\n# ///\n'
PREFIX = "import sys\nprint(sys.prefix)\n"


def test_a_script_with_no_metadata_runs_isolated(tmp_path):
    (tmp_path / "run.py").write_text(PREFIX)
    assert run_windows_task.script_command("uv", tmp_path) == [
        "uv", "run", "--isolated", "--script", "run.py"]


def test_a_script_with_its_own_header_keeps_its_own_environment(tmp_path):
    (tmp_path / "run.py").write_text(HEADER + PREFIX)
    assert run_windows_task.script_command("uv", tmp_path) == ["uv", "run", "--script", "run.py"]


def test_pdt_run_starts_the_app_through_the_same_command(tmp_path, monkeypatch):
    from pdt import cli
    seen = []
    monkeypatch.setattr(cli.lifecycle, "run_job",
                        lambda name, provider, command, folder, *rest: seen.append(command) or 0)
    project = tmp_path / "project"
    (project / "hr").mkdir(parents=True)
    (project / "pdt.yml").write_text("platform:\n  provider: windows\n")
    (project / "hr" / "config.yml").write_text("schedule: daily\n")
    (project / "hr" / "run.py").write_text(PREFIX)
    monkeypatch.chdir(project)
    monkeypatch.delenv("PDT_PROJECT", raising=False)
    from types import SimpleNamespace
    assert cli.cmd_run(SimpleNamespace(app="hr", deployed=False, follow=False, kill=False)) == 0
    assert seen == [["uv", "run", "--isolated", "--script", "run.py"]]


@pytest.mark.skipif(shutil.which("uv") is None, reason="needs uv")
@pytest.mark.parametrize("header", ["", HEADER])
def test_a_venv_in_a_parent_folder_is_not_used(tmp_path, header):
    env = {key: value for key, value in os.environ.items() if key != "VIRTUAL_ENV"}
    env["UV_OFFLINE"] = "1"
    made = subprocess.run(["uv", "venv", "-q", "--python", sys.executable, str(tmp_path / ".venv")],
                          env=env, capture_output=True, text=True)
    assert made.returncode == 0, made.stderr
    app = tmp_path / "project" / "hr"
    app.mkdir(parents=True)
    (app.parent / "pdt.yml").write_text("apps: []\n")
    (app / "run.py").write_text(header + PREFIX)
    proc = subprocess.run(run_windows_task.script_command("uv", app), cwd=app, env=env,
                          capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    assert Path(proc.stdout.strip()).resolve() != (tmp_path / ".venv").resolve()
