import json
import os
import shutil
import subprocess
import time

import pytest

from conftest import add_app, plain
from pdt import __version__, powershell
from pdt.config import validate_app
from pdt.deploy_common import (
    DOCKERFILE, ENTRYPOINT, POWERSHELL_DOCKERFILE, context_ignore_text, image_action, run_build,
    write_dockerfile)


def staged(tmp_path, name, own=None):
    app_dir = tmp_path / "project" / name
    app_dir.mkdir(parents=True)
    (app_dir / "run.py").write_text("def main():\n    return 0\n")
    if own is not None:
        (app_dir / "Dockerfile").write_text(own)
    stage = tmp_path / "stage"
    stage.mkdir()
    return {"name": name, "dir": app_dir}, stage


def test_an_app_without_a_dockerfile_gets_the_generated_one(tmp_path):
    app, stage = staged(tmp_path, "my-report")
    write_dockerfile(stage, app)
    assert (stage / "Dockerfile").read_text() == DOCKERFILE.format(app="my-report")
    assert image_action(app, "build image x") == "build image x with Docker on this computer"
    assert plain([image_action(app, "build image x", "Cloud Build")]) == [
        "build image x in the cloud with Cloud Build"]


def test_the_generated_dockerfile_installs_from_the_script_header_before_the_rest(tmp_path):
    app, stage = staged(tmp_path, "my-report")
    header = '# /// script\n# dependencies = ["pdt-cli==0.1.9"]\n# ///\n'
    (app["dir"] / "run.py").write_text(header + "def main():\n    return 0\n")
    (app["dir"] / "run.py.lock").write_text("lock\n")
    (app["dir"] / "pdt_cli-0.1.11-py3-none-any.whl").write_text("wheel\n")
    write_dockerfile(stage, app)
    assert (stage / ".pdt-script" / "run.py").read_text() == header
    assert (stage / ".pdt-script" / "run.py.lock").read_text() == "lock\n"
    assert (stage / ".pdt-script" / "pdt_cli-0.1.11-py3-none-any.whl").read_text() == "wheel\n"
    text = (stage / "Dockerfile").read_text()
    assert (text.index("WORKDIR /workspace/my-report\n") < text.index("COPY .pdt-script .\n")
            < text.index("RUN uv sync --script run.py\n") < text.index("COPY . /workspace\n"))


def test_a_run_py_without_a_script_header_is_staged_whole(tmp_path):
    app, stage = staged(tmp_path, "my-report")
    write_dockerfile(stage, app)
    assert sorted(path.name for path in (stage / ".pdt-script").iterdir()) == ["run.py"]
    assert (stage / ".pdt-script" / "run.py").read_text() == "def main():\n    return 0\n"


def test_an_app_dockerfile_is_used_as_is_at_the_context_root(tmp_path):
    own = "FROM python:3.12-slim\nCOPY . /workspace\n"
    app, stage = staged(tmp_path, "my-report", own)
    write_dockerfile(stage, app)
    assert (stage / "Dockerfile").read_text() == own
    assert not (stage / ".pdt-script").exists()
    assert plain([image_action(app, "build image x")]) == [
        "build image x with Docker on this computer (from my-report/Dockerfile)"]


def app_with_dockerfile(project, platform_text):
    (project / "pdt.yml").write_text(platform_text)
    folder = add_app(project, "my-report", "schedule: daily\n")
    (folder / "Dockerfile").write_text("FROM python:3.12-slim\n")


def test_a_dockerfile_is_fine_on_every_cloud_provider(project):
    app_with_dockerfile(project, "platform:\n  provider: azure\n")
    assert validate_app("my-report") == []
    (project / "pdt.yml").write_text("platform:\n  provider: google-cloud\n")
    assert validate_app("my-report") == []
    (project / "pdt.yml").write_text("platform:\n  provider: aws\n  account: '123456789012'\n")
    assert validate_app("my-report") == []


def test_a_leftover_runtime_key_is_told_to_go(project):
    (project / "pdt.yml").write_text("platform:\n  provider: azure\n  runtime: container_apps\n")
    add_app(project, "my-report", "schedule: daily\n")
    assert validate_app("my-report") == [
        "pdt.yml: platform: 'runtime' is no longer a setting: AWS always runs jobs on "
        "Fargate and Azure on Container Apps Jobs; remove the key"]


def test_a_dockerfile_for_the_cloud_does_not_stop_a_windows_app(project):
    app_with_dockerfile(project, "platform:\n  provider: windows\n")
    assert validate_app("my-report") == []


def test_an_app_dockerignore_is_moved_to_the_context_root(tmp_path):
    app, stage = staged(tmp_path, "my-report", "FROM python:3.12-slim\n")
    (app["dir"] / ".dockerignore").write_text("# local state\n.meltano\n/output\n!output/keep\n\n")
    write_dockerfile(stage, app)
    expected = "# local state\nmy-report/.meltano\nmy-report/output\n!my-report/output/keep\n\n"
    assert (stage / ".dockerignore").read_text() == expected


def test_no_dockerignore_means_no_ignore_files(tmp_path):
    app, stage = staged(tmp_path, "my-report")
    write_dockerfile(stage, app)
    assert not (stage / ".dockerignore").exists()


def test_context_ignore_text_keeps_comments_and_blank_lines():
    assert context_ignore_text("#c\n\nx\n", "a") == "#c\n\na/x\n"


@pytest.mark.skipif(shutil.which("sh") is None, reason="needs a POSIX shell")
def test_the_entrypoint_runs_the_app_in_lifecycle_and_ends_the_log_with_the_exit_code(tmp_path):
    (tmp_path / "uv").write_text("#!/bin/sh\necho \"uv $*\"\nexit 3\n")
    (tmp_path / "uv").chmod(0o755)
    line = next(line for line in DOCKERFILE.format(app="report").splitlines()
                if line.startswith("ENTRYPOINT "))
    command = json.loads(line.removeprefix("ENTRYPOINT "))
    proc = subprocess.run(command, capture_output=True, text=True,
                          env={"PATH": f"{tmp_path}:/usr/bin:/bin"})
    assert proc.stdout == ("uv run --script /opt/pdt-lifecycle/pdt/lifecycle.py -- "
                           "uv run --script run.py\npdt: exit 3\n")
    assert proc.returncode == 3



@pytest.mark.skipif(os.name == "nt" or shutil.which("sh") is None, reason="needs a POSIX shell")
def test_the_entrypoint_passes_sigterm_on_and_still_ends_the_log_with_the_exit_code(tmp_path):
    ready = tmp_path / "ready"
    (tmp_path / "uv").write_text(
        f"#!/bin/sh\ntrap 'echo stopped; exit 143' TERM\ntouch {ready}\nwhile :; do sleep 0.1; done\n")
    (tmp_path / "uv").chmod(0o755)
    line = next(line for line in DOCKERFILE.format(app="report").splitlines()
                if line.startswith("ENTRYPOINT "))
    proc = subprocess.Popen(json.loads(line.removeprefix("ENTRYPOINT ")), stdout=subprocess.PIPE,
                            text=True, env={"PATH": f"{tmp_path}:/usr/bin:/bin"})
    while not ready.exists():
        time.sleep(0.05)
    proc.terminate()
    assert proc.communicate(timeout=10)[0] == "stopped\npdt: exit 143\n"
    assert proc.returncode == 143

@pytest.fixture
def powershell_app(tmp_path, monkeypatch):
    scans = []

    def scan(app, provider):
        scans.append(provider)
        return powershell.ScriptScan(["report.ps1"], [], list(app["modules"]), [])

    monkeypatch.setattr(powershell, "scan", scan)
    app_dir = tmp_path / "project" / "my-report"
    app_dir.mkdir(parents=True)
    (app_dir / "report.ps1").write_text("Write-Output hi\n")
    stage = tmp_path / "stage"
    stage.mkdir()
    app = {"name": "my-report", "dir": app_dir, "platform": {"provider": "aws"}, "modules": []}
    return app, stage, scans


def test_a_powershell_app_gets_the_powershell_dockerfile_with_its_modules(powershell_app):
    app, stage, scans = powershell_app
    app["modules"] = [powershell.ModuleNeed("ImportExcel", "7.8.6", "#Requires in report.ps1")]
    write_dockerfile(stage, app)
    text = (stage / "Dockerfile").read_text()
    command = powershell.install_command(app["modules"])
    install = f'RUN {json.dumps(["pwsh", "-NoProfile", "-Command", command])}\n'
    assert text == POWERSHELL_DOCKERFILE.format(
        app="my-report", version=__version__, modules=install, sync="",
        start="/opt/pdt/bin/python -m pdt.run_powershell .")
    assert scans == ["aws"]
    pdt_line = f'RUN uv venv /opt/pdt && uv pip install --python /opt/pdt "pdt-cli[apps]=={__version__}"\n'
    pwsh_line = 'RUN pwsh="$(/opt/pdt/bin/python -m pdt.pwsh)" && ln -s "$pwsh" /usr/local/bin/pwsh'
    assert text.index(pdt_line) < text.index(pwsh_line) < text.index(install)
    assert "curl" not in text


def test_a_powershell_app_without_modules_has_no_empty_install_step(powershell_app):
    app, stage, _scans = powershell_app
    write_dockerfile(stage, app)
    text = (stage / "Dockerfile").read_text()
    assert "-Command" not in text
    assert all(line.strip() != "RUN" for line in text.splitlines())
    assert "\nCOPY . /workspace\n" in text


def test_a_powershell_app_with_its_own_run_py_runs_it_after_installing_its_modules(powershell_app):
    app, stage, _scans = powershell_app
    app["modules"] = [powershell.ModuleNeed("ImportExcel", "7.8.6", "requirements.psd1")]
    (app["dir"] / "run.py").write_text("")
    (app["dir"] / "requirements.psd1").write_text("@{ 'ImportExcel' = '7.8.6' }\n")
    write_dockerfile(stage, app)
    text = (stage / "Dockerfile").read_text()
    tail = ("WORKDIR /workspace/my-report\nCOPY .pdt-script .\nRUN uv sync --script run.py\n"
            "COPY . /workspace\n" + ENTRYPOINT.replace("{start}", "uv run --script run.py").format())
    assert text.endswith(tail)
    assert text.index("Install-PSResource") < text.index("RUN uv sync")
    assert "pdt.run_powershell" not in text


@pytest.mark.skipif(os.name == "nt" or shutil.which("sh") is None,
                    reason="runs a Linux image's entrypoint with POSIX paths")
def test_the_powershell_entrypoint_runs_the_app_in_lifecycle_with_its_exit_code(
        tmp_path, powershell_app):
    app, stage, _scans = powershell_app
    write_dockerfile(stage, app)
    python = tmp_path / "opt" / "pdt" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text("#!/bin/sh\necho \"args: $*\"\nexit 4\n")
    python.chmod(0o755)
    uv = tmp_path / "uv"
    uv.write_text("#!/bin/sh\necho \"uv $*\"\nwhile [ \"$1\" != -- ]; do shift; done\nshift\nexec \"$@\"\n")
    uv.chmod(0o755)
    text = (stage / "Dockerfile").read_text()
    line = next(line for line in text.splitlines() if line.startswith("ENTRYPOINT "))
    command = json.loads(line.removeprefix("ENTRYPOINT "))
    command[-1] = command[-1].replace("/opt/pdt/bin/python", str(python))
    proc = subprocess.run(command, capture_output=True, text=True,
                          env={"PATH": f"{tmp_path}:/usr/bin:/bin"})
    assert proc.stdout == (f"uv run --script /opt/pdt-lifecycle/pdt/lifecycle.py -- {python} "
                           "-m pdt.run_powershell .\nargs: -m pdt.run_powershell .\npdt: exit 4\n")
    assert proc.returncode == 4


def test_python_writes_each_line_as_it_prints_it_in_every_image():
    # A held stdout would put a print() line after a later stderr line in the log.
    for dockerfile in (DOCKERFILE, POWERSHELL_DOCKERFILE):
        env = next(line for line in dockerfile.splitlines() if line.startswith("ENV "))
        assert "PYTHONUNBUFFERED=1" in env.split()


def test_a_failed_build_that_streamed_its_output_stops_with_the_command(monkeypatch, capsys):
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 1))
    with pytest.raises(SystemExit):
        run_build(["docker", "build", "."])
    assert "docker build . failed" in capsys.readouterr().out
