import pytest

from pdt import deploy_azure_container_apps as azure


def test_gitlab_builds_and_pushes_with_docker(tmp_path, monkeypatch):
    stage = tmp_path / "stage"
    stage.mkdir()
    calls = []
    monkeypatch.setenv("GITLAB_CI", "true")
    monkeypatch.setattr(azure, "stage_build_context", lambda app: stage)
    monkeypatch.setattr(azure, "run_quiet", lambda *args: calls.append(args))
    monkeypatch.setattr(azure, "run_stream", lambda *args: calls.append(args))
    monkeypatch.setattr(azure, "run_build", lambda args: calls.append(tuple(args)))
    azure.build_image({"name": "report"}, "pdtregistry", "report")
    image = "pdtregistry.azurecr.io/report:latest"
    assert calls == [
        ("acr", "login", "--name", "pdtregistry"),
        ("docker", "build", "--platform", "linux/amd64", "-t", image, str(stage)),
        ("docker", "push", image),
    ]
    assert not stage.exists()


def test_outside_gitlab_uses_azure_builds(tmp_path, monkeypatch):
    stage = tmp_path / "stage"
    stage.mkdir()
    calls = []
    monkeypatch.delenv("GITLAB_CI", raising=False)
    monkeypatch.setattr(azure, "stage_build_context", lambda app: stage)
    monkeypatch.setattr(azure, "run_stream", lambda *args: calls.append(args))
    azure.build_image({"name": "report"}, "pdtregistry", "report")
    assert calls == [
        ("acr", "build", "--registry", "pdtregistry", "--image", "report:latest", str(stage)),
    ]
    assert not stage.exists()


def test_a_failed_docker_build_does_not_push_and_removes_the_context(tmp_path, monkeypatch):
    stage = tmp_path / "stage"
    stage.mkdir()
    calls = []

    def build(command):
        calls.append(command[:2])
        raise SystemExit(1)

    monkeypatch.setenv("GITLAB_CI", "true")
    monkeypatch.setattr(azure, "stage_build_context", lambda app: stage)
    monkeypatch.setattr(azure, "run_quiet", lambda *args: None)
    monkeypatch.setattr(azure, "run_build", build)
    with pytest.raises(SystemExit):
        azure.build_image({"name": "report"}, "pdtregistry", "report")
    assert calls == [["docker", "build"]]
    assert not stage.exists()
