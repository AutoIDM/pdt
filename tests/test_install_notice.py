from pathlib import Path

import pytest

from pdt import deploy


def probe_answers(monkeypatch, code):
    probes = []

    def run(command, **kwargs):
        probes.append(command)
        return deploy.subprocess.CompletedProcess(command, code)

    monkeypatch.setattr(deploy.subprocess, "run", run)
    return probes


def test_the_first_azure_run_says_it_installs_the_azure_cli(monkeypatch, capsys):
    probes = probe_answers(monkeypatch, 1)
    deploy.announce_install(Path("src/pdt/deploy_azure.py"))
    assert "Installing the Azure CLI. This happens once" in capsys.readouterr().out
    assert probes[0][:2] == ["uv", "sync"] and "--offline" in probes[0]


def test_an_installed_azure_cli_starts_without_a_word(monkeypatch, capsys):
    probe_answers(monkeypatch, 0)
    deploy.announce_install(Path("src/pdt/deploy_azure.py"))
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("script", ["deploy_aws.py", "deploy_google_cloud.py"])
def test_a_quick_install_is_not_probed(monkeypatch, script):
    monkeypatch.setattr(deploy.subprocess, "run", lambda *args, **kwargs: pytest.fail("probed"))
    deploy.announce_install(Path(script))
