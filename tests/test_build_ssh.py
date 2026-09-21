from pdt import deploy_azure_container_apps as azure
from pdt.deploy_common import ssh_build_args


def test_no_agent_means_no_ssh_flag(monkeypatch):
    monkeypatch.delenv("SSH_AUTH_SOCK", raising=False)
    assert ssh_build_args() == []


def test_a_running_agent_is_forwarded(monkeypatch):
    monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/agent.sock")
    assert ssh_build_args() == ["--ssh", "default"]


def test_gitlab_docker_build_forwards_the_agent(tmp_path, monkeypatch):
    stage = tmp_path / "stage"
    stage.mkdir()
    app_dir = tmp_path / "report"
    app_dir.mkdir()
    calls = []
    monkeypatch.setenv("GITLAB_CI", "true")
    monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/agent.sock")
    monkeypatch.setattr(azure, "stage_build_context", lambda app: stage)
    monkeypatch.setattr(azure, "run_quiet", lambda *args: None)
    monkeypatch.setattr(azure, "run_build", lambda args: calls.append(tuple(args)))
    azure.build_image({"name": "report", "dir": app_dir}, "pdtregistry", "report")
    assert calls[0][:4] == ("docker", "build", "--ssh", "default")
