import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from pdt import deploy_aws_fargate as aws
from pdt import deploy_azure_container_apps as azure
from pdt import deploy_google_cloud as google
from pdt import deploy_windows as windows
from pdt.deploy_common import NotDeployed, Run, RunState


APP = {"name": "report", "platform": {"region": "us-east-1", "account": "123"}}


def test_aws_remote_job_transport(monkeypatch):
    class Ecs:
        statuses = [
            {"lastStatus": "RUNNING"},
            {"lastStatus": "STOPPED", "containers": [{"exitCode": 0}]},
        ]

        def run_task(self, **_kwargs):
            return {"tasks": [{"taskArn": "arn:task/run-1"}]}

        def describe_tasks(self, **_kwargs):
            return {"tasks": [self.statuses.pop(0)]}

    class Logs:
        def get_log_events(self, **kwargs):
            assert kwargs.get("nextToken") == "next"
            return {"events": [{"message": "line two"}], "nextForwardToken": "later"}

    monkeypatch.setattr(aws, "ensure_session", lambda app: object())
    monkeypatch.setattr(aws, "aws_settings", lambda app, session: ("123", "us-east-1"))
    monkeypatch.setattr(aws, "fargate_clients", lambda session: {"ecs": Ecs(), "logs": Logs(), "ec2": object()})
    monkeypatch.setattr(aws, "default_network", lambda ec2: (["subnet"], "security-group"))
    job = aws.remote_job(APP)
    run = job.start()
    assert run.id == "run-1"
    assert job.poll(run).state is RunState.RUNNING
    assert job.poll(run).state is RunState.SUCCEEDED
    assert job.read_logs(run, "next").cursor == "later"

    class Missing(Ecs):
        def run_task(self, **_kwargs):
            error = Exception()
            error.response = {"Error": {"Code": "ClientException", "Message": "TaskDefinition not found."}}
            raise error

    monkeypatch.setattr(aws, "fargate_clients", lambda session: {"ecs": Missing(), "logs": Logs(), "ec2": object()})
    with pytest.raises(NotDeployed):
        aws.remote_job(APP).start()

    class Broken(Missing):
        def run_task(self, **_kwargs):
            error = Exception()
            error.response = {"Error": {"Code": "ClientException", "Message": "bad request"}}
            raise error

    monkeypatch.setattr(aws, "fargate_clients", lambda session: {"ecs": Broken(), "logs": Logs(), "ec2": object()})
    with pytest.raises(Exception) as exc:
        aws.remote_job(APP).start()
    assert exc.value.response["Error"]["Message"] == "bad request"


def test_azure_remote_job_transport(monkeypatch):
    monkeypatch.setattr(azure, "azure_settings", lambda app: {"resource_group": "pdt", "subscription": "sub"})
    responses = [
        SimpleNamespace(returncode=0, stdout=json.dumps({"name": "run-1"}), stderr=""),
    ]
    monkeypatch.setattr(azure.subprocess, "run", lambda *_args, **_kwargs: responses.pop(0))
    values = [
        {"properties": {"status": "Running"}},
        {"properties": {"status": "Succeeded"}},
        [{"timestamp": "one", "message": "line one"}],
        [{"timestamp": "one", "message": "line one"}, {"timestamp": "two", "message": "line two"}],
    ]
    monkeypatch.setattr(azure, "az_json", lambda *_args: values.pop(0))
    job = azure.remote_job(APP)
    run = job.start()
    assert run.id == "run-1"
    assert job.poll(run).state is RunState.RUNNING
    assert job.poll(run).state is RunState.SUCCEEDED
    first = job.read_logs(run, None)
    second = job.read_logs(run, first.cursor)
    assert first.lines == ["line one"]
    assert second.lines == ["line two"]

    monkeypatch.setattr(azure.subprocess, "run", lambda *_args, **_kwargs: SimpleNamespace(returncode=1, stdout="", stderr="ResourceNotFound"))
    with pytest.raises(NotDeployed):
        azure.remote_job(APP).start()

    monkeypatch.setattr(azure.subprocess, "run", lambda *_args, **_kwargs: SimpleNamespace(returncode=1, stdout="", stderr="quota exceeded"))
    with pytest.raises(SystemExit):
        azure.remote_job(APP).start()


def test_google_remote_job_transport(monkeypatch):
    calls = []
    responses = [
        SimpleNamespace(returncode=0, stdout=json.dumps({"metadata": {"name": "run-1"}}), stderr=""),
        SimpleNamespace(returncode=0, stdout=json.dumps({"status": {"runningCount": 1}}), stderr=""),
        SimpleNamespace(returncode=0, stdout=json.dumps({"status": {"conditions": [{"type": "Completed", "status": "True"}]}}), stderr=""),
        SimpleNamespace(returncode=0, stdout=json.dumps([{"timestamp": "one", "textPayload": "line one"}]), stderr=""),
        SimpleNamespace(returncode=0, stdout=json.dumps([{"timestamp": "two", "textPayload": "line two"}]), stderr=""),
    ]

    def run(args, **_kwargs):
        calls.append(args)
        return responses.pop(0)

    monkeypatch.setattr(google.subprocess, "run", run)
    job = google.remote_job({"name": "report", "platform": {"project": "project", "region": "region"}})
    started = job.start()
    assert started.id == "run-1"
    assert job.poll(started).state is RunState.RUNNING
    assert job.poll(started).state is RunState.SUCCEEDED
    first = job.read_logs(started, None)
    assert job.read_logs(started, first.cursor).lines == ["line two"]
    assert 'timestamp>"one"' in calls[-1][3]

    monkeypatch.setattr(google.subprocess, "run", lambda *_args, **_kwargs: SimpleNamespace(returncode=1, stdout="", stderr="NOT_FOUND"))
    with pytest.raises(NotDeployed):
        google.remote_job({"name": "report", "platform": {"project": "project"}}).start()

    monkeypatch.setattr(google.subprocess, "run", lambda *_args, **_kwargs: SimpleNamespace(returncode=1, stdout="", stderr="permission denied"))
    with pytest.raises(SystemExit):
        google.remote_job({"name": "report", "platform": {"project": "project"}}).start()


def test_windows_remote_job_transport(tmp_path, monkeypatch):
    folder = tmp_path / ".pdt" / "logs" / "report"
    folder.mkdir(parents=True)
    monkeypatch.setattr(windows.config, "find_project", lambda: tmp_path)
    monkeypatch.setattr(windows, "_preflight", lambda require_uv=False: ("powershell", None))
    monkeypatch.setattr(windows, "_task_state", lambda *_args: "managed")
    monkeypatch.setattr(windows.time, "sleep", lambda _seconds: None)

    def start_task(*_args, **_kwargs):
        (folder / "run-1.log").write_text("line one\n")
        return True

    monkeypatch.setattr(windows, "_run", start_task)
    polls = [SimpleNamespace(returncode=0, stdout="Running\nafter\n0\n", stderr=""),
             SimpleNamespace(returncode=0, stdout="Ready\nafter\n0\n", stderr="")]
    monkeypatch.setattr(windows.subprocess, "run", lambda *_args, **_kwargs: polls.pop(0))
    job = windows.remote_job({"name": "report"})
    started = job.start()
    assert started.id == "run-1"
    assert job.poll(started).state is RunState.RUNNING
    assert job.poll(started).state is RunState.SUCCEEDED
    first = job.read_logs(started, None)
    (folder / "run-1.log").write_text("line one\nline two\n")
    assert job.read_logs(started, first.cursor).lines == ["line two"]

    monkeypatch.setattr(windows, "_task_state", lambda *_args: "absent")
    with pytest.raises(NotDeployed):
        windows.remote_job({"name": "report"}).start()

    monkeypatch.setattr(windows, "_task_state", lambda *_args: "managed")
    monkeypatch.setattr(windows, "_run", lambda *_args, **_kwargs: (_ for _ in ()).throw(windows.WindowsDeployError("failed")))
    with pytest.raises(windows.WindowsDeployError):
        windows.remote_job({"name": "report"}).start()
