from pathlib import Path

import pytest

from pdt import deploy, deploy_aws_batch, lifecycle

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


def test_an_mr_run_on_a_provider_with_no_mr_verb_says_so(monkeypatch, capsys):
    monkeypatch.setattr(deploy, "_load", lambda name: ({"dir": Path("."), "name": name}, "azure"))
    assert deploy.mr_run("hr", "12") == 1
    assert "no merge request run" in capsys.readouterr().out
