import pytest

from pdt import deploy_azure_container_apps as azure


def test_builds_with_docker_and_pushes_to_acr(tmp_path, monkeypatch):
    stage = tmp_path / "stage"
    stage.mkdir()
    calls = []
    monkeypatch.delenv("SSH_AUTH_SOCK", raising=False)
    monkeypatch.setattr(azure, "run_quiet", lambda *args: calls.append(args) or '{"accessToken": "t", "loginServer": "pdtregistry.azurecr.io"}')
    monkeypatch.setattr(azure, "run_build", lambda args, **_: calls.append(tuple(args)))
    azure.build_image(stage, {"registry": "pdtregistry"}, "report", False)
    image = "pdtregistry.azurecr.io/report:latest"
    assert calls == [
        ("acr", "login", "--name", "pdtregistry", "--expose-token"),
        ("docker", "login", "--username", "00000000-0000-0000-0000-000000000000",
         "--password-stdin", "pdtregistry.azurecr.io"),
        ("docker", "build", "--platform", "linux/amd64", "-t", image, str(stage)),
        ("docker", "push", image),
    ]


def test_a_failed_docker_build_does_not_push(tmp_path, monkeypatch):
    stage = tmp_path / "stage"
    stage.mkdir()
    calls = []

    def build(command, **_):
        calls.append(command[:2])
        if command[1] == "build":
            raise SystemExit(1)

    monkeypatch.setattr(azure, "run_quiet", lambda *args: '{"accessToken": "t", "loginServer": "pdtregistry.azurecr.io"}')
    monkeypatch.setattr(azure, "run_build", build)
    with pytest.raises(SystemExit):
        azure.build_image(stage, {"registry": "pdtregistry"}, "report", False)
    assert calls == [["docker", "login"], ["docker", "build"]]

