"""Where a cloud deploy builds its image: Docker on this computer, or the provider's cloud builder."""

import json
import subprocess
from types import SimpleNamespace

import pytest
from rich.text import Text

from conftest import add_app
from pdt import config, deploy_common
from pdt.deploy_common import BINFMT, build_in_cloud, docker_problem, emulate, ssh_dependency

HEADER = '''# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "pdt-cli==0.1.7",
#     {dependency}
# ]
# ///
def main():
    return 0
'''

SSH_DOCKERFILE = "FROM x\nRUN --mount=type=ssh uv sync --script run.py\n"


def said(capsys) -> str:
    return Text.from_markup(capsys.readouterr().out).plain.replace("\n", " ")


def docker(monkeypatch, on_path=True, returncode=0, stderr="", hangs=False):
    """A docker command as `docker_problem` sees it. Returns the commands run."""
    commands = []
    monkeypatch.setattr(deploy_common.shutil, "which",
                        lambda name: "/usr/bin/docker" if on_path and name == "docker" else None)

    def run(command, **kwargs):
        commands.append(command)
        if hangs:
            raise subprocess.TimeoutExpired(command, kwargs["timeout"])
        return SimpleNamespace(returncode=returncode, stdout="", stderr=stderr)

    monkeypatch.setattr(deploy_common.subprocess, "run", run)
    return commands


def app_for(tmp_path, setting="auto", source="", run_py="def main():\n    return 0\n",
            dockerfile=None):
    folder = tmp_path / "report"
    folder.mkdir(exist_ok=True)
    (folder / "run.py").write_text(run_py)
    if dockerfile is not None:
        (folder / "Dockerfile").write_text(dockerfile)
    return {"name": "report", "dir": folder, "docker_build": setting, "docker_build_from": source}


def test_docker_that_answers_has_no_problem(monkeypatch):
    commands = docker(monkeypatch)
    assert docker_problem() == ""
    assert commands == [["docker", "info"]]


def test_no_docker_command_is_the_reason(monkeypatch):
    commands = docker(monkeypatch, on_path=False)
    assert docker_problem() == "the docker command is not on the PATH"
    assert commands == []


def test_a_daemon_that_does_not_answer_is_the_reason(monkeypatch):
    stderr = ("Client: Docker Engine\n"
              "Cannot connect to the Docker daemon at unix:///var/run/docker.sock. "
              "Is the docker daemon running?\n")
    docker(monkeypatch, returncode=1, stderr=stderr)
    assert docker_problem() == (
        "docker info failed: Cannot connect to the Docker daemon at "
        "unix:///var/run/docker.sock. Is the docker daemon running?")


def test_a_docker_info_that_hangs_is_the_reason(monkeypatch):
    docker(monkeypatch, hangs=True)
    assert docker_problem() == "docker info did not answer within 20 seconds"


def test_docker_found_builds_on_this_computer(tmp_path, monkeypatch, capsys):
    docker(monkeypatch)
    emulated = []
    monkeypatch.setattr(deploy_common, "emulate", lambda platform: emulated.append(platform) or "")
    assert build_in_cloud(app_for(tmp_path), "CodeBuild", "linux/arm64") is False
    assert "Docker found: docker info answered, so Docker on this computer builds the image." in (
        said(capsys))
    assert emulated == ["linux/arm64"]


@pytest.mark.parametrize("on_path, reason", [
    (False, "the docker command is not on the PATH"),
    (True, "docker info failed: Is the docker daemon running?"),
])
def test_no_docker_builds_in_the_cloud_and_says_why(tmp_path, monkeypatch, capsys, on_path, reason):
    docker(monkeypatch, on_path=on_path, returncode=1, stderr="Is the docker daemon running?")
    monkeypatch.setattr(deploy_common, "emulate", lambda platform: pytest.fail("no local build"))
    assert build_in_cloud(app_for(tmp_path), "ACR Tasks", "linux/amd64") is True
    assert (f"Docker on this computer cannot build the image: {reason}. ACR Tasks builds the "
            "image in the cloud.") in said(capsys)


def test_local_without_docker_stops_and_names_the_file(tmp_path, monkeypatch, capsys):
    docker(monkeypatch, on_path=False)
    app = app_for(tmp_path, "local", "report/config.yml")
    with pytest.raises(SystemExit):
        build_in_cloud(app, "Cloud Build", "linux/amd64")
    out = said(capsys)
    assert ("Docker on this computer cannot build the image: the docker command is not on the "
            "PATH.") in out
    assert "docker-build: local in report/config.yml builds only with Docker." in out


def test_cloud_never_asks_docker(tmp_path, monkeypatch, capsys):
    commands = docker(monkeypatch)
    app = app_for(tmp_path, "cloud", "pdt.yml")
    assert build_in_cloud(app, "Cloud Build", "linux/amd64") is True
    assert commands == []
    assert "docker-build: cloud in pdt.yml, so Cloud Build builds the image in the cloud." in (
        said(capsys))


def test_an_emulator_that_cannot_be_added_builds_in_the_cloud(tmp_path, monkeypatch, capsys):
    docker(monkeypatch)
    monkeypatch.setattr(deploy_common, "emulate", lambda platform: "it cannot run arm64")
    assert build_in_cloud(app_for(tmp_path), "CodeBuild", "linux/arm64") is True
    assert ("Docker on this computer cannot build the image: it cannot run arm64. CodeBuild "
            "builds the image in the cloud.") in said(capsys)


def test_local_with_an_emulator_that_cannot_be_added_stops(tmp_path, monkeypatch, capsys):
    docker(monkeypatch)
    monkeypatch.setattr(deploy_common, "emulate", lambda platform: "it cannot run arm64")
    with pytest.raises(SystemExit):
        build_in_cloud(app_for(tmp_path, "local", "pdt.yml"), "CodeBuild", "linux/arm64")
    assert "cannot build the image: it cannot run arm64" in said(capsys)


def test_an_own_dockerfile_with_an_ssh_mount_needs_the_agent(tmp_path):
    app = app_for(tmp_path, dockerfile=SSH_DOCKERFILE)
    assert ssh_dependency(app) == "the RUN --mount=type=ssh step in report/Dockerfile"


@pytest.mark.parametrize("setting, source, hint", [
    ("auto", "", "A build with Docker on this computer can: start Docker and run the same"),
    ("cloud", "pdt.yml", "start Docker, remove docker-build: cloud from pdt.yml, and run the same"),
])
def test_a_cloud_build_stops_on_an_ssh_dependency(tmp_path, monkeypatch, capsys, setting, source,
                                                   hint):
    docker(monkeypatch, on_path=False)
    app = app_for(tmp_path, setting, source, dockerfile=SSH_DOCKERFILE)
    with pytest.raises(SystemExit):
        build_in_cloud(app, "CodeBuild", "linux/arm64")
    out = said(capsys)
    assert ("report needs the RUN --mount=type=ssh step in report/Dockerfile, which installs over SSH. "
            "A build in the cloud with CodeBuild cannot use the SSH agent of this computer.") in out
    assert hint in out


def test_a_local_build_keeps_an_ssh_dependency(tmp_path, monkeypatch):
    docker(monkeypatch)
    monkeypatch.setattr(deploy_common, "emulate", lambda platform: "")
    app = app_for(tmp_path, dockerfile=SSH_DOCKERFILE)
    assert build_in_cloud(app, "CodeBuild", "linux/arm64") is False


def test_docker_build_merges_from_project_to_app(project):
    (project / "pdt.yml").write_text(
        "docker-build: cloud\nplatform:\n  provider: aws\n  account: '123456789012'\n"
        "apps:\n  - name: report\n    docker-build: local\n")
    add_app(project, "report", "schedule: daily\n")
    add_app(project, "other", "schedule: daily\n")
    assert config.merged_app("other")["docker_build"] == "cloud"
    assert config.merged_app("other")["docker_build_from"] == "pdt.yml"
    assert config.merged_app("report")["docker_build"] == "local"
    (project / "report" / "config.yml").write_text("schedule: daily\ndocker-build: auto\n")
    app = config.merged_app("report")
    assert (app["docker_build"], app["docker_build_from"]) == ("auto", "report/config.yml")
    assert config.validate_app("report") == []


def test_docker_build_defaults_to_auto(project):
    add_app(project, "report", "schedule: daily\n")
    app = config.merged_app("report")
    assert (app["docker_build"], app["docker_build_from"]) == ("auto", "")


def test_an_unknown_docker_build_names_the_file_and_the_choices(project):
    add_app(project, "report", "schedule: daily\ndocker-build: remote\n")
    assert config.validate_app("report") == [
        "report/config.yml: docker-build must be auto, local, or cloud"]


def test_docker_build_on_the_windows_provider_is_told_to_go(project):
    (project / "pdt.yml").write_text("platform:\n  provider: windows\n")
    add_app(project, "report", "schedule: daily\ndocker-build: cloud\n")
    assert config.validate_app("report") == [
        "report/config.yml: docker-build: not used by the windows provider; remove the key"]


def docker_runs(monkeypatch, results):
    commands = []

    def run(command, **_kwargs):
        commands.append(command)
        code = results.pop(0) if command[:2] == ["docker", "run"] and "true" in command else 0
        return SimpleNamespace(returncode=code)

    monkeypatch.setattr(subprocess, "run", run)
    return commands


def test_a_platform_docker_runs_needs_no_emulator(monkeypatch, capsys):
    commands = docker_runs(monkeypatch, [0])
    assert emulate("linux/amd64") == ""
    assert len(commands) == 1
    assert said(capsys) == ""


def test_a_platform_docker_cannot_run_gets_the_emulator(monkeypatch, capsys):
    commands = docker_runs(monkeypatch, [1, 0])
    assert emulate("linux/arm64") == ""
    assert ["docker", "run", "--privileged", "--rm", BINFMT, "--install", "arm64"] in commands
    assert "Added the arm64 emulator" in said(capsys)


def test_an_emulator_that_does_not_help_is_the_reason(monkeypatch):
    docker_runs(monkeypatch, [1, 1])
    reason = Text.from_markup(emulate("linux/arm64")).plain
    assert reason.startswith("it cannot run linux/arm64 programs, and docker run --privileged")
