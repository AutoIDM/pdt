import subprocess

import pytest

from pdt import deploy_azure
from pdt.deploy_common import fail_command


def test_a_failed_command_shows_its_output_the_full_command_and_the_hint(capsys):
    proc = subprocess.CompletedProcess([], 2, "partial output", "ERROR: the real reason")
    with pytest.raises(SystemExit):
        fail_command(["docker", "build", "-t", "repo/image:tag", "/tmp/my context"], proc,
                     hint="Start Docker and run the same command again.")
    lines = capsys.readouterr().out.splitlines()
    assert lines[:2] == ["partial output", "ERROR: the real reason"]
    assert lines[2] == ("error: docker build -t repo/image:tag '/tmp/my context' failed "
                        "with exit code 2; fix the problem above and re-run")
    assert lines[3] == "Start Docker and run the same command again."


def test_a_streamed_command_prints_only_the_error_line(capsys):
    with pytest.raises(SystemExit):
        fail_command(["pdt", "az", "acr", "build"], subprocess.CompletedProcess([], 1))
    assert capsys.readouterr().out.splitlines() == [
        "error: pdt az acr build failed with exit code 1; fix the problem above and re-run"]


def test_an_az_failure_names_every_argument(monkeypatch, capsys):
    stderr = ("ERROR: (InvalidResourceGroupLocation) Invalid resource group location 'eastus'. "
              "The Resource group already exists in location 'eastus2'.")
    monkeypatch.setattr(deploy_azure.subprocess, "run",
                        lambda command, **kwargs: subprocess.CompletedProcess(command, 1, "", stderr))
    with pytest.raises(SystemExit):
        deploy_azure.run_quiet("group", "create", "--name", "pdt-shared", "--location", "eastus")
    out = capsys.readouterr().out
    assert stderr in out
    assert "pdt az group create --name pdt-shared --location eastus failed" in out
