import json
import os
import shutil
import subprocess

import pytest

from conftest import add_app
from pdt import __version__, powershell
from pdt.config import validate_app
from pdt.deploy_common import (
    DOCKERFILE, POWERSHELL_DOCKERFILE, context_ignore_text, image_action, write_dockerfile)


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
    assert image_action(app, "build image x") == "build image x"


def test_an_app_dockerfile_is_used_as_is_at_the_context_root(tmp_path):
    own = "FROM python:3.12-slim\nCOPY . /workspace\n"
    app, stage = staged(tmp_path, "my-report", own)
    write_dockerfile(stage, app)
    assert (stage / "Dockerfile").read_text() == own
    assert image_action(app, "build image x") == "build image x (from my-report/Dockerfile)"


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


def test_a_dockerfile_on_a_provider_without_images_is_flagged(project):
    app_with_dockerfile(project, "platform:\n  provider: windows\n")
    assert validate_app("my-report") == [
        "my-report/Dockerfile: not used by the windows provider; remove the file"]


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
def test_the_entrypoint_ends_the_log_with_the_exit_code(tmp_path):
    (tmp_path / "uv").write_text("#!/bin/sh\necho working\nexit 3\n")
    (tmp_path / "uv").chmod(0o755)
    line = next(line for line in DOCKERFILE.splitlines() if line.startswith("ENTRYPOINT "))
    command = json.loads(line.removeprefix("ENTRYPOINT "))
    proc = subprocess.run(command, capture_output=True, text=True,
                          env={"PATH": f"{tmp_path}:/usr/bin:/bin"})
    assert proc.stdout == "working\npdt: exit 3\n"
    assert proc.returncode == 3


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
    tail = ("WORKDIR /workspace/my-report\nRUN uv sync --script run.py\n"
            'ENTRYPOINT ["sh", "-c", "uv run --script run.py; code=$?; '
            'echo \\"pdt: exit $code\\"; exit $code"]\n')
    assert text.endswith(tail)
    assert text.index("Install-PSResource") < text.index("RUN uv sync")
    assert "pdt.run_powershell" not in text


@pytest.mark.skipif(os.name == "nt" or shutil.which("sh") is None,
                    reason="runs a Linux image's entrypoint with POSIX paths")
def test_the_powershell_entrypoint_ends_the_log_with_the_exit_code(tmp_path, powershell_app):
    app, stage, _scans = powershell_app
    write_dockerfile(stage, app)
    python = tmp_path / "opt" / "pdt" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text("#!/bin/sh\necho \"args: $*\"\nexit 4\n")
    python.chmod(0o755)
    text = (stage / "Dockerfile").read_text()
    line = next(line for line in text.splitlines() if line.startswith("ENTRYPOINT "))
    command = json.loads(line.removeprefix("ENTRYPOINT "))
    command[-1] = command[-1].replace("/opt/pdt/bin/python", str(python))
    proc = subprocess.run(command, capture_output=True, text=True,
                          env={"PATH": "/usr/bin:/bin"})
    assert proc.stdout == "args: -m pdt.run_powershell .\npdt: exit 4\n"
    assert proc.returncode == 4
