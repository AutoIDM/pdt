import json
from pathlib import Path

import pytest

from pdt import (deploy, deploy_aws_batch, deploy_azure_container_apps, deploy_google_cloud,
                 lifecycle, runs_cli)

PRODUCTION = {"jobDefinitionArn": "arn:def:1", "revision": 1,
              "tags": {"managed-by": "pdt"},
              "containerProperties": {"image": "repo:hr-latest", "jobRoleArn": "role",
                                      "environment": [{"name": "PDT_STORAGE_URL", "value": "s3://b/hr/"},
                                                      {"name": "PDT_TRIGGER", "value": "manual"}],
                                      "secrets": [{"name": "PDT_ENV_JSON", "valueFrom": "arn:secret"}]}}


class Clients(dict):
    def __init__(self, fail_submit=False):
        super().__init__()
        self.calls = []
        owner = self

        class Batch:
            def register_job_definition(self, **kwargs):
                owner.calls.append(("register", kwargs))
                return {"jobDefinitionArn": "arn:mr:1"}

            def submit_job(self, **kwargs):
                owner.calls.append(("submit", kwargs))
                if fail_submit:
                    raise RuntimeError("no capacity")
                return {"jobId": "job-1"}

            def deregister_job_definition(self, jobDefinition):
                owner.calls.append(("deregister", jobDefinition))

            def terminate_job(self, jobId, reason):
                owner.calls.append(("terminate", jobId))

        class Ecr:
            def batch_delete_image(self, repositoryName, imageIds):
                owner.calls.append(("delete image", imageIds))

        self.update(batch=Batch(), ecr=Ecr())


@pytest.fixture
def aws(monkeypatch):
    def make(fail_submit=False):
        clients = Clients(fail_submit)
        monkeypatch.setattr(deploy_aws_batch, "batch_clients", lambda session: clients)
        monkeypatch.setattr(deploy_aws_batch, "active_job_definitions",
                            lambda batch, name: [PRODUCTION])
        monkeypatch.setattr(deploy_aws_batch, "aws_settings", lambda app, session: ("1", "us-east-2"))
        monkeypatch.setattr(deploy_aws_batch, "build_in_cloud", lambda *args: False)
        monkeypatch.setattr(deploy_aws_batch, "ensure_repository", lambda ecr: "repo")
        monkeypatch.setattr(deploy_aws_batch, "ensure_image",
                            lambda app, image, *args: clients.calls.append(("build", image)))
        monkeypatch.setattr(deploy_aws_batch, "follow_job", lambda *args: 0)
        monkeypatch.setenv("PDT_RUN_ID", "cmd_1")
        monkeypatch.setenv("PDT_APP_ARGS", "--full")
        return clients
    return make


class Session:
    def client(self, name, config=None):
        return None


def test_aws_mr_run_registers_the_mr_job_from_the_deployed_one_and_deletes_it(aws):
    clients = aws()
    assert deploy_aws_batch.mr_run({"name": "hr"}, Session(), "12") == 0
    names = [call[0] for call in clients.calls]
    assert names == ["build", "register", "submit", "deregister", "delete image"]
    assert clients.calls[0][1] == "repo:hr-mr12"
    registered = clients.calls[1][1]
    assert registered["jobDefinitionName"] == "pdt-hr-mr12"
    container = registered["containerProperties"]
    assert container["image"] == "repo:hr-mr12" and container["secrets"] == PRODUCTION[
        "containerProperties"]["secrets"]
    env = {item["name"]: item["value"] for item in container["environment"]}
    assert env == {"PDT_STORAGE_URL": "s3://b/hr/", lifecycle.TRIGGER_ENV: "mr",
                   lifecycle.MR_IID_ENV: "12", "PDT_RUN_ID": "cmd_1", "PDT_APP_ARGS": "--full"}
    assert clients.calls[2][1]["jobName"] == "pdt-hr-mr12"
    assert clients.calls[4][1] == [{"imageTag": "hr-mr12"}]


def test_aws_mr_run_cleans_up_after_a_failure(aws):
    clients = aws(fail_submit=True)
    with pytest.raises(RuntimeError):
        deploy_aws_batch.mr_run({"name": "hr"}, Session(), "12")
    assert [call[0] for call in clients.calls][-2:] == ["deregister", "delete image"]
    assert "terminate" not in [call[0] for call in clients.calls]


def test_aws_mr_run_stops_its_job_when_the_follow_fails(aws, monkeypatch):
    clients = aws()

    def broken(*args):
        raise RuntimeError("log read failed")

    monkeypatch.setattr(deploy_aws_batch, "follow_job", broken)
    with pytest.raises(RuntimeError):
        deploy_aws_batch.mr_run({"name": "hr"}, Session(), "12")
    assert [call[0] for call in clients.calls][-3:] == ["terminate", "deregister", "delete image"]
    assert ("terminate", "job-1") in clients.calls


def test_an_mr_run_on_the_windows_provider_says_so(monkeypatch, capsys):
    monkeypatch.setattr(deploy, "_load", lambda name: ({"dir": Path("."), "name": name}, "windows"))
    assert deploy.mr_run("hr", "12") == 1
    assert "no merge request run" in capsys.readouterr().out


AZURE_JOB = {"properties": {"template": {"containers": [{
    "name": "hr", "image": "reg.azurecr.io/hr@sha256:abc",
    "resources": {"cpu": 0.25, "memory": "0.5Gi"},
    "env": [{"name": "PDT_ENV_JSON", "secretRef": "env-json"},
            {"name": "PDT_STORAGE_URL", "value": "az://s/hr/"},
            {"name": "PDT_TRIGGER", "value": "manual"}]}]}}}


def azure(monkeypatch, fail_start=False, status="Succeeded"):
    calls = []

    def run_quiet(*args, **_kwargs):
        calls.append(args)
        if fail_start and args[:3] == ("containerapp", "job", "start"):
            raise RuntimeError("no capacity")
        return '{"name": "pdt-hr-x1"}'

    monkeypatch.setattr(deploy_azure_container_apps, "find_job",
                        lambda settings, name: ("pdt-hr", AZURE_JOB))
    monkeypatch.setattr(deploy_azure_container_apps, "build_in_cloud", lambda *args: False)
    monkeypatch.setattr(deploy_azure_container_apps, "ensure_image",
                        lambda app, image, *args: calls.append(("build", image)))
    monkeypatch.setattr(deploy_azure_container_apps, "run_quiet", run_quiet)
    monkeypatch.setattr(deploy_azure_container_apps, "az_json",
                        lambda *args: {"properties": {"status": status}})
    monkeypatch.setattr(deploy_azure_container_apps, "logs", lambda *args, **kwargs: 0)
    monkeypatch.setenv("PDT_RUN_ID", "cmd_1")
    monkeypatch.setenv("PDT_APP_ARGS", "--full")
    return calls


SETTINGS = {"registry": "reg", "resource_group": "pdt"}


def test_azure_mr_run_starts_one_execution_with_the_mr_image_and_deletes_it(monkeypatch):
    calls = azure(monkeypatch)
    assert deploy_azure_container_apps.mr_run({"name": "hr"}, SETTINGS, "12") == 0
    assert calls[0] == ("build", "reg.azurecr.io/hr-mr12:latest")
    start = calls[1]
    assert start[:5] == ("containerapp", "job", "start", "--name", "pdt-hr")
    assert start[start.index("--image") + 1] == "reg.azurecr.io/hr-mr12:latest"
    env = list(start[start.index("--env-vars") + 1:start.index("--output")])
    assert env == ["PDT_ENV_JSON=secretref:env-json", "PDT_STORAGE_URL=az://s/hr/",
                   "PDT_TRIGGER=mr", "PDT_MR_IID=12", "PDT_RUN_ID=cmd_1", "PDT_APP_ARGS=--full"]
    assert calls[2] == ("acr", "repository", "delete", "--name", "reg", "--repository", "hr-mr12",
                        "--yes")
    assert not any(call[:3] in (("containerapp", "job", "update"), ("containerapp", "job", "create"))
                   for call in calls)


def test_azure_mr_run_deletes_the_image_after_a_failure(monkeypatch):
    calls = azure(monkeypatch, fail_start=True)
    with pytest.raises(RuntimeError):
        deploy_azure_container_apps.mr_run({"name": "hr"}, SETTINGS, "12")
    assert calls[-1][:3] == ("acr", "repository", "delete")


def test_azure_mr_run_stops_its_execution_when_the_follow_fails(monkeypatch):
    calls = azure(monkeypatch, status="Running")

    def broken(*args, **kwargs):
        raise RuntimeError("log read failed")

    monkeypatch.setattr(deploy_azure_container_apps, "logs", broken)
    with pytest.raises(RuntimeError):
        deploy_azure_container_apps.mr_run({"name": "hr"}, SETTINGS, "12")
    assert ("containerapp", "job", "stop", "--name", "pdt-hr", "--resource-group", "pdt",
            "--job-execution-name", "pdt-hr-x1") in calls
    assert calls[-1][:3] == ("acr", "repository", "delete")


def test_azure_mr_run_stops_its_execution_when_the_follow_is_interrupted(monkeypatch):
    calls = azure(monkeypatch, status="Running")
    monkeypatch.setattr(deploy_azure_container_apps, "logs", lambda *args, **kwargs: 130)
    assert deploy_azure_container_apps.mr_run({"name": "hr"}, SETTINGS, "12") == 130
    assert [call[:3] for call in calls[-2:]] == [("containerapp", "job", "stop"),
                                                  ("acr", "repository", "delete")]


def test_azure_mr_run_leaves_a_finished_execution_alone(monkeypatch):
    calls = azure(monkeypatch, status="Failed")
    monkeypatch.setattr(deploy_azure_container_apps, "logs", lambda *args, **kwargs: 1)
    assert deploy_azure_container_apps.mr_run({"name": "hr"}, SETTINGS, "12") == 1
    assert ("containerapp", "job", "stop") not in [call[:3] for call in calls]


GOOGLE_JOB = {"spec": {"template": {"spec": {"template": {"spec": {
    "serviceAccountName": "pdt-hr@p.iam.gserviceaccount.com",
    "containers": [{"image": "img@sha", "env": [
        {"name": "PDT_ENV_JSON", "valueFrom": {"secretKeyRef": {"name": "pdt-hr-env",
                                                               "key": "latest"}}},
        {"name": "PDT_STORAGE_URL", "value": "gs://b/hr/"},
        {"name": "PDT_TRIGGER", "value": "manual"}]}]}}}}}}
EXECUTION = {"metadata": {"name": "pdt-hr-mr12-abc", "creationTimestamp": "2026-11-04T00:00:00Z"},
             "status": {"startTime": "2026-11-04T00:00:01Z", "completionTime": "2026-11-04T00:01:00Z",
                        "conditions": [{"type": "Completed", "status": "True"}]}}


def google(monkeypatch, fail_execute=False):
    calls = []

    def run_quiet(*args, **_kwargs):
        calls.append(args)
        if args[:3] == ("run", "jobs", "execute"):
            if fail_execute:
                raise RuntimeError("quota")
            return json.dumps(EXECUTION)
        return ""

    monkeypatch.setattr(deploy_google_cloud, "project_region", lambda app: ("p", "us-east1"))
    monkeypatch.setattr(deploy_google_cloud, "preflight", lambda app, project, yes: project)
    monkeypatch.setattr(deploy_google_cloud, "read_json_or_none", lambda *args: GOOGLE_JOB)
    monkeypatch.setattr(deploy_google_cloud, "build_in_cloud", lambda *args: False)
    monkeypatch.setattr(deploy_google_cloud, "ensure_image",
                        lambda app, image, *args: calls.append(("build", image)))
    monkeypatch.setattr(deploy_google_cloud, "run_quiet", run_quiet)
    monkeypatch.setattr(deploy_google_cloud, "read_lines", lambda project, execution: [])
    monkeypatch.setattr(deploy_google_cloud, "list_runs", lambda project, region, job: [])
    monkeypatch.setenv("PDT_RUN_ID", "cmd_1")
    monkeypatch.delenv("PDT_APP_ARGS", raising=False)
    monkeypatch.delenv("PDT_ARTIFACT_REGISTRY_REPO", raising=False)
    return calls


def test_google_mr_run_makes_a_job_of_its_own_runs_it_once_and_deletes_it(monkeypatch):
    calls = google(monkeypatch)
    assert deploy_google_cloud.mr_run({"name": "hr"}, True, "12") == 0
    image = "us-east1-docker.pkg.dev/p/pdt/hr-mr12:latest"
    assert calls[0] == ("build", image)
    create = calls[1]
    assert create[:4] == ("run", "jobs", "create", "pdt-hr-mr12")
    assert create[create.index("--image") + 1] == image
    assert create[create.index("--set-env-vars") + 1] == (
        "^\x1f^PDT_STORAGE_URL=gs://b/hr/\x1fPDT_TRIGGER=mr\x1fPDT_MR_IID=12\x1fPDT_RUN_ID=cmd_1")
    assert create[create.index("--set-secrets") + 1] == "PDT_ENV_JSON=pdt-hr-env:latest"
    assert create[create.index("--service-account") + 1] == "pdt-hr@p.iam.gserviceaccount.com"
    assert calls[2][:4] == ("run", "jobs", "execute", "pdt-hr-mr12")
    assert calls[3][:4] == ("run", "jobs", "delete", "pdt-hr-mr12")
    assert calls[4][:5] == ("artifacts", "docker", "images", "delete", image)
    assert all("pdt-hr" not in call[3:4] for call in calls if call[:2] == ("run", "jobs")
               and call[2] in ("update", "deploy"))
    assert not any(call[0] == "scheduler" for call in calls)


def test_google_mr_run_deletes_the_job_and_the_image_after_a_failure(monkeypatch):
    calls = google(monkeypatch, fail_execute=True)
    with pytest.raises(RuntimeError):
        deploy_google_cloud.mr_run({"name": "hr"}, True, "12")
    assert [call[:3] for call in calls[-2:]] == [("run", "jobs", "delete"),
                                                  ("artifacts", "docker", "images")]


def test_google_mr_run_keeps_a_comma_inside_one_env_value(monkeypatch):
    calls = google(monkeypatch)
    monkeypatch.setenv("PDT_APP_ARGS", "--ids 1,2")
    deploy_google_cloud.mr_run({"name": "hr"}, True, "12")
    value = calls[1][calls[1].index("--set-env-vars") + 1]
    delimiter, _, rest = value[1:].partition("^")
    assert "PDT_APP_ARGS=--ids 1,2" in rest.split(delimiter)


def test_google_mr_run_stops_its_execution_when_the_run_fails(monkeypatch):
    calls = google(monkeypatch, fail_execute=True)
    running = runs_cli.Run("pdt-hr-mr12-abc", None, None, "running")
    monkeypatch.setattr(deploy_google_cloud, "list_runs",
                        lambda project, region, job: [running] if job == "pdt-hr-mr12" else [])
    with pytest.raises(RuntimeError):
        deploy_google_cloud.mr_run({"name": "hr"}, True, "12")
    assert ("run", "jobs", "executions", "cancel", "pdt-hr-mr12-abc") in [call[:5] for call in calls]
    assert [call[:3] for call in calls[-2:]] == [("run", "jobs", "delete"),
                                                  ("artifacts", "docker", "images")]
