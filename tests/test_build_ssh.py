from pdt import deploy_azure_container_apps as azure
from pdt.deploy_common import ssh_build_args


def test_no_agent_means_no_ssh_flag(monkeypatch):
    monkeypatch.delenv("SSH_AUTH_SOCK", raising=False)
    assert ssh_build_args() == []


def test_a_running_agent_is_forwarded(tmp_path, monkeypatch):
    socket = tmp_path / "agent.sock"
    socket.touch()
    monkeypatch.setenv("SSH_AUTH_SOCK", str(socket))
    assert ssh_build_args() == ["--ssh", "default"]


def test_a_stale_agent_socket_is_not_forwarded(tmp_path, monkeypatch):
    monkeypatch.setenv("SSH_AUTH_SOCK", str(tmp_path / "gone.sock"))
    assert ssh_build_args() == []


def test_github_actions_docker_build_forwards_the_agent(tmp_path, monkeypatch):
    stage = tmp_path / "stage"
    stage.mkdir()
    app_dir = tmp_path / "report"
    app_dir.mkdir()
    socket = tmp_path / "agent.sock"
    socket.touch()
    calls = []
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("SSH_AUTH_SOCK", str(socket))
    monkeypatch.setattr(azure, "stage_build_context", lambda app: stage)
    monkeypatch.setattr(azure, "run_quiet", lambda *args: None)
    monkeypatch.setattr(azure, "run_build", lambda args: calls.append(tuple(args)))
    azure.build_image({"name": "report", "dir": app_dir}, "pdtregistry", "report")
    assert calls[0][:6] == ("docker", "build", "--platform", "linux/amd64", "--ssh", "default")
