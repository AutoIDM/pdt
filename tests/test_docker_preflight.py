from types import SimpleNamespace

import pytest

from pdt import deploy_aws_batch, deploy_azure_container_apps, deploy_common, deploy_google_cloud

# Each provider's deploy, the name its message uses, and its first call that reaches the cloud.
PROVIDERS = [
    (deploy_aws_batch, "AWS", "ensure_session"),
    (deploy_azure_container_apps, "Azure", "azure_settings"),
    (deploy_google_cloud, "Google Cloud", "project_region"),
]


def docker(monkeypatch, path, info_returncode):
    monkeypatch.setattr(deploy_common.shutil, "which", lambda name: path)
    monkeypatch.setattr(deploy_common.subprocess, "run",
                        lambda *args, **kwargs: SimpleNamespace(returncode=info_returncode))


@pytest.mark.parametrize("module, provider, first_cloud_call", PROVIDERS)
@pytest.mark.parametrize("path, info_returncode, message", [
    (None, 0, "Docker is not installed. pdt builds the image for {provider}"),
    ("/usr/bin/docker", 1, "Docker is installed but not running."),
])
def test_deploy_stops_before_the_cloud_without_docker(
        monkeypatch, capsys, module, provider, first_cloud_call, path, info_returncode, message):
    docker(monkeypatch, path, info_returncode)
    monkeypatch.setattr(module, first_cloud_call, lambda *args: pytest.fail("reached the cloud"))
    with pytest.raises(SystemExit):
        module.deploy({"name": "report"}, assume_yes=True)
    assert message.format(provider=provider) in " ".join(capsys.readouterr().out.split())


def test_a_missing_docker_says_pdt_cannot_install_it(monkeypatch, capsys):
    docker(monkeypatch, None, 0)
    with pytest.raises(SystemExit):
        deploy_common.docker_preflight("Google Cloud")
    out = " ".join(capsys.readouterr().out.split())
    assert "the one tool pdt cannot install for you" in out
    assert "https://www.docker.com/products/docker-desktop/" in out


def test_a_running_docker_passes(monkeypatch):
    docker(monkeypatch, "/usr/bin/docker", 0)
    deploy_common.docker_preflight("AWS")
