from datetime import timedelta

from conftest import add_app
from pdt import (deploy_aws_batch, deploy_azure_container_apps, deploy_google_cloud,
                 deploy_windows, runs_cli)


def captured(monkeypatch):
    calls = []
    monkeypatch.setattr(runs_cli, "logs", lambda *args, **kwargs: calls.append(kwargs) or 0)
    return calls


class Session:
    def client(self, name):
        return name


def test_every_cloud_waits_5_minutes_for_its_log_store(monkeypatch):
    calls = captured(monkeypatch)
    monkeypatch.setattr(deploy_google_cloud, "project_region", lambda app: ("p", "r"))
    monkeypatch.setattr(deploy_google_cloud, "preflight", lambda app, project, yes: project)
    monkeypatch.setattr(deploy_azure_container_apps, "find_job", lambda settings, name: ("job", None))
    app = {"name": "report"}
    deploy_aws_batch.logs(app, Session(), [])
    deploy_google_cloud.logs(app, [], False)
    deploy_azure_container_apps.logs(app, {}, [])
    assert [call["delay"] for call in calls] == [timedelta(minutes=5)] * 3


def test_windows_reads_a_local_file_with_no_delay(project, monkeypatch):
    (project / "pdt.yml").write_text("platform:\n  provider: windows\n")
    add_app(project, "report")
    calls = captured(monkeypatch)
    monkeypatch.setattr(deploy_windows.sys, "platform", "win32")
    monkeypatch.setattr("sys.argv", ["deploy_windows.py", "logs", "report"])
    assert deploy_windows.main() == 0
    assert "delay" not in calls[0]
