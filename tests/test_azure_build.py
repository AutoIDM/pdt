import pytest

from pdt import deploy_azure_container_apps as azure


def app_at(tmp_path):
    """An app folder with no Dockerfile of its own, so the generated one is written."""
    app_dir = tmp_path / "report"
    app_dir.mkdir()
    return {"name": "report", "dir": app_dir}


def test_github_actions_builds_and_pushes_with_docker(tmp_path, monkeypatch):
    stage = tmp_path / "stage"
    stage.mkdir()
    calls = []
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setattr(azure, "stage_build_context", lambda app: stage)
    monkeypatch.setattr(azure, "run_quiet", lambda *args: calls.append(args))
    monkeypatch.setattr(azure, "run_stream", lambda *args: calls.append(args))
    monkeypatch.setattr(azure, "run_build", lambda args: calls.append(tuple(args)))
    azure.build_image(app_at(tmp_path), "pdtregistry", "report")
    image = "pdtregistry.azurecr.io/report:latest"
    assert calls == [
        ("acr", "login", "--name", "pdtregistry"),
        ("docker", "build", "--platform", "linux/amd64", "-t", image, str(stage)),
        ("docker", "push", image),
    ]
    assert not stage.exists()


def test_outside_github_actions_uses_azure_builds(tmp_path, monkeypatch):
    stage = tmp_path / "stage"
    stage.mkdir()
    calls = []
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.setattr(azure, "stage_build_context", lambda app: stage)
    monkeypatch.setattr(azure, "run_stream", lambda *args: calls.append(args))
    azure.build_image(app_at(tmp_path), "pdtregistry", "report")
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

    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setattr(azure, "stage_build_context", lambda app: stage)
    monkeypatch.setattr(azure, "run_quiet", lambda *args: None)
    monkeypatch.setattr(azure, "run_build", build)
    with pytest.raises(SystemExit):
        azure.build_image(app_at(tmp_path), "pdtregistry", "report")
    assert calls == [["docker", "build"]]
    assert not stage.exists()
