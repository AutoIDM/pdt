import pytest

from pdt import deploy_google_cloud as google

IMAGE = "us-central1-docker.pkg.dev/my-project/pdt/report:latest"


def app_at(tmp_path):
    """An app folder with no Dockerfile of its own, so the generated one is written."""
    app_dir = tmp_path / "report"
    app_dir.mkdir()
    return {"name": "report", "dir": app_dir}


def fake_build(monkeypatch, tmp_path, build):
    stage = tmp_path / "stage"
    stage.mkdir()
    monkeypatch.setattr(google, "stage_build_context", lambda app: stage)
    monkeypatch.setattr(google, "run_quiet", lambda *args: "ya29.token\n"
                        if args == ("auth", "print-access-token") else pytest.fail(args))
    monkeypatch.setattr(google, "run_build", build)
    return stage


def test_logs_in_builds_for_amd64_and_pushes_to_artifact_registry(tmp_path, monkeypatch):
    calls = []
    stage = fake_build(monkeypatch, tmp_path,
                       lambda command, data=None: calls.append((command, data)))
    google.build_image(app_at(tmp_path), IMAGE, "us-central1")
    assert calls == [
        (["docker", "login", "--username", "oauth2accesstoken", "--password-stdin",
          "https://us-central1-docker.pkg.dev"], "ya29.token"),
        (["docker", "build", "--platform", "linux/amd64", "-t", IMAGE, str(stage)], None),
        (["docker", "push", IMAGE], None),
    ]
    assert not stage.exists()


def test_a_failed_docker_build_does_not_push_and_removes_the_context(tmp_path, monkeypatch):
    calls = []

    def build(command, data=None):
        calls.append(command[:2])
        if command[1] == "build":
            raise SystemExit(1)

    stage = fake_build(monkeypatch, tmp_path, build)
    with pytest.raises(SystemExit):
        google.build_image(app_at(tmp_path), IMAGE, "us-central1")
    assert calls == [["docker", "login"], ["docker", "build"]]
    assert not stage.exists()


def test_deploy_no_longer_needs_cloud_build():
    assert "cloudbuild.googleapis.com" not in google.APIS
