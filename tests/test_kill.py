import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from pdt import (deploy, deploy_aws_batch, deploy_azure_container_apps, deploy_google_cloud,
                 deploy_windows, run_windows_task, runs_cli)

NOW = datetime(2026, 11, 4, tzinfo=timezone.utc)


def run(identity, status):
    return runs_cli.Run(identity, NOW, None if status == "running" else NOW, status)


class Batch:
    def __init__(self):
        self.terminated = []

    def terminate_job(self, jobId, reason):
        self.terminated.append(jobId)


class Session:
    def __init__(self, batch):
        self.batch = batch

    def client(self, name, config=None):
        return self.batch


def test_aws_kill_terminates_each_job_that_runs(monkeypatch):
    batch = Batch()
    monkeypatch.setattr(deploy_aws_batch, "list_runs", lambda _batch, job: [
        run("job-1", "running"), run("job-2", "succeeded"), run("job-3", "running")])
    assert deploy_aws_batch.kill({"name": "hr"}, Session(batch)) == 0
    assert batch.terminated == ["job-1", "job-3"]


def test_aws_kill_with_nothing_running_exits_0(monkeypatch, capsys):
    monkeypatch.setattr(deploy_aws_batch, "list_runs", lambda _batch, job: [run("job-1", "failed")])
    assert deploy_aws_batch.kill({"name": "hr"}, Session(Batch())) == 0
    assert "No job of hr runs" in capsys.readouterr().out


def test_the_deployer_policy_lets_a_kill_terminate_a_job():
    assert "batch:TerminateJob" in deploy_aws_batch.BATCH_ACTIONS


def test_azure_kill_stops_each_running_execution(monkeypatch):
    calls = []
    monkeypatch.setattr(deploy_azure_container_apps, "list_runs", lambda _settings, job: [
        run("pdt-hr-a", "running"), run("pdt-hr-b", "succeeded")])
    monkeypatch.setattr(deploy_azure_container_apps, "run_quiet",
                        lambda *args, **_kwargs: calls.append(args) or "")
    assert deploy_azure_container_apps.kill({"name": "hr"}, {"resource_group": "pdt"}) == 0
    assert calls == [("containerapp", "job", "stop", "--name", "pdt-hr", "--resource-group",
                      "pdt", "--job-execution-name", "pdt-hr-a")]


def test_google_kill_cancels_each_running_execution(monkeypatch):
    calls = []
    monkeypatch.setattr(deploy_google_cloud, "project_region", lambda _app: ("proj", "us-east1"))
    monkeypatch.setattr(deploy_google_cloud, "preflight", lambda _app, project, _yes: project)
    monkeypatch.setattr(deploy_google_cloud, "list_runs", lambda project, region, job: [
        run("pdt-hr-x", "running"), run("pdt-hr-y", "failed")])
    monkeypatch.setattr(deploy_google_cloud, "run_quiet",
                        lambda *args, **_kwargs: calls.append(args) or "")
    assert deploy_google_cloud.kill({"name": "hr"}, True) == 0
    assert calls == [("run", "jobs", "executions", "cancel", "pdt-hr-x", "--region", "us-east1",
                      "--project", "proj", "--quiet")]


def test_windows_kill_stops_the_task(monkeypatch, capsys):
    scripts = []
    monkeypatch.setattr(deploy_windows, "_preflight", lambda require_uv=True: ("powershell.exe", None))
    monkeypatch.setattr(deploy_windows, "_task_state", lambda powershell, name: "managed")
    monkeypatch.setattr(deploy_windows, "_task_running", lambda powershell, name: True)
    monkeypatch.setattr(deploy_windows, "_run",
                        lambda powershell, script, **kwargs: scripts.append((script, kwargs)) or True)
    assert deploy_windows.kill({"name": "hr"}) == 0
    assert scripts == [("Stop-ScheduledTask -TaskName 'pdt-hr' -ErrorAction Stop",
                        {"elevate": True})]
    assert "Stopped hr" in capsys.readouterr().out


def test_windows_kill_of_a_task_that_is_not_running_stops_nothing(monkeypatch, capsys):
    scripts = []
    monkeypatch.setattr(deploy_windows, "_preflight", lambda require_uv=True: ("powershell.exe", None))
    monkeypatch.setattr(deploy_windows, "_task_state", lambda powershell, name: "managed")
    monkeypatch.setattr(deploy_windows, "_task_running", lambda powershell, name: False)
    monkeypatch.setattr(deploy_windows, "_run",
                        lambda powershell, script, **kwargs: scripts.append((script, kwargs)) or True)
    assert deploy_windows.kill({"name": "hr"}) == 0
    assert scripts == []
    output = capsys.readouterr().out
    assert "Stopped" not in output and "No run of hr runs" in output


def test_pdt_run_kill_sends_kill_to_the_provider(monkeypatch):
    sent = []
    monkeypatch.setattr(deploy, "_load", lambda name: ({"dir": Path("."), "name": name}, "aws"))
    monkeypatch.setattr(deploy.config, "is_deployed", lambda name: True)
    monkeypatch.setattr(deploy.config, "load_env", lambda folder: [])
    monkeypatch.setattr(deploy, "dispatch", lambda *args: sent.append(args) or 0)
    assert deploy.kill("hr") == 0
    assert sent == [("aws", "kill", "hr", False)]


@pytest.mark.skipif(os.name == "nt", reason="off Windows the wrapper makes no job object")
def test_the_job_object_code_imports_and_makes_no_job_off_windows():
    assert run_windows_task.hold_in_job() is None


TREE = """
import subprocess, sys, time
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
print(child.pid, flush=True)
time.sleep(120)
"""


@pytest.mark.skipif(os.name != "nt", reason="a job object is a Windows object")
def test_ending_the_job_ends_the_whole_tree():
    job = run_windows_task.new_job()
    proc = subprocess.Popen([sys.executable, "-c", TREE], stdout=subprocess.PIPE, text=True,
                            creationflags=0x4)
    run_windows_task.assign(job, proc._handle)
    import ctypes
    ctypes.WinDLL("ntdll").NtResumeProcess(ctypes.c_void_p(int(proc._handle)))
    grandchild = int(proc.stdout.readline())
    run_windows_task.end_job(job)
    proc.wait(10)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        found = subprocess.run(["tasklist", "/FI", f"PID eq {grandchild}"], capture_output=True,
                               text=True).stdout
        if str(grandchild) not in found:
            break
        time.sleep(0.2)
    assert str(grandchild) not in found
