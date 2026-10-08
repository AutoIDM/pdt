import json
import shutil
import subprocess

import pytest

from conftest import add_app
from pdt.config import validate_app
from pdt.deploy_common import (
    DOCKERFILE, context_ignore_text, image_action, write_dockerfile)


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


def test_the_generated_dockerfile_installs_the_deploying_notice_script(tmp_path):
    app, stage = staged(tmp_path, "my-report")
    write_dockerfile(stage, app)
    dockerfile = (stage / "Dockerfile").read_text()
    assert "COPY .pdt-runtime/pdt /opt/pdt/pdt" in dockerfile
    assert "uv sync --script /opt/pdt/pdt/notify.py" in dockerfile
    assert "uv run --script /opt/pdt/pdt/notify.py --app-dir /workspace/my-report" in dockerfile
    assert "export PDT_APP=my-report PDT_RUN_ID=$run_id" in dockerfile
    assert "uv export" not in dockerfile


def test_the_notice_uses_each_cloud_provider_run_identifier():
    dockerfile = DOCKERFILE.format(app="my-report")
    assert "AWS_BATCH_JOB_ID" in dockerfile
    assert "CONTAINER_APP_JOB_EXECUTION_NAME" in dockerfile
    assert "CLOUD_RUN_EXECUTION" in dockerfile


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
    assert (stage / ".gcloudignore").read_text() == expected


def test_no_dockerignore_means_no_ignore_files(tmp_path):
    app, stage = staged(tmp_path, "my-report")
    write_dockerfile(stage, app)
    assert not (stage / ".dockerignore").exists()
    assert not (stage / ".gcloudignore").exists()


def test_context_ignore_text_keeps_comments_and_blank_lines():
    assert context_ignore_text("#c\n\nx\n", "a") == "#c\n\na/x\n"


@pytest.mark.skipif(shutil.which("sh") is None, reason="needs a POSIX shell")
def test_the_entrypoint_ends_the_log_with_the_exit_code(tmp_path):
    (tmp_path / "uv").write_text("#!/bin/sh\necho working\nexit 3\n")
    (tmp_path / "uv").chmod(0o755)
    dockerfile = DOCKERFILE.format(app="my-report")
    line = next(line for line in dockerfile.splitlines() if line.startswith("ENTRYPOINT "))
    command = json.loads(line.removeprefix("ENTRYPOINT "))
    proc = subprocess.run(command, capture_output=True, text=True,
                          env={"PATH": f"{tmp_path}:/usr/bin:/bin"})
    assert proc.stdout == "working\nworking\npdt: exit 3\n"
    assert proc.returncode == 3
