import json
import subprocess

import pytest
from botocore.exceptions import ClientError

from pdt import deploy, deploy_aws, deploy_azure, deploy_common, grants
from pdt.deploy_aws import GRANT_BOUNDARY, GRANT_LEFT_OUT, GRANT_ROLE

ACCOUNT = "123456789012"
AWS_PLACE = f"aws:{ACCOUNT}:us-east-2"
AZURE_PLACE = "azure:00000000-0000-0000-0000-000000000001:eastus2"
GOOGLE_PLACE = "google-cloud:my-project-1:us-central1"


def not_found(*_args, **_kwargs):
    raise ClientError({"Error": {"Code": "NoSuchEntity", "Message": "none"}}, "x")


class FakeIam:
    """IAM in memory, enough for a grant: each call appends to `calls`."""

    def __init__(self):
        self.calls, self.policies, self.role, self.attached = [], {}, None, []

    def __getattr__(self, name):
        def call(**kwargs):
            self.calls.append((name, kwargs))
            return {}
        return call

    def get_policy(self, PolicyArn):
        if PolicyArn not in self.policies:
            not_found()
        return {"Policy": {"Tags": [{"Key": "managed-by", "Value": "pdt"}]}}

    def create_policy(self, PolicyName, PolicyDocument, Description, Tags):
        self.calls.append(("create_policy", {"PolicyName": PolicyName, "Tags": Tags}))
        self.policies[f"arn:aws:iam::{ACCOUNT}:policy/{PolicyName}"] = json.loads(PolicyDocument)

    def list_policy_versions(self, PolicyArn):
        return {"Versions": [{"VersionId": "v1", "IsDefaultVersion": True}]}

    def get_role(self, RoleName):
        if self.role is None:
            not_found()
        return {"Role": self.role}

    def create_role(self, **kwargs):
        self.calls.append(("create_role", kwargs))
        self.role = {"Arn": "x", "Tags": kwargs["Tags"], **kwargs}
        return {"Role": self.role}

    def attach_role_policy(self, RoleName, PolicyArn):
        self.attached.append(PolicyArn)

    def list_attached_role_policies(self, RoleName):
        return {"AttachedPolicies": [{"PolicyArn": arn, "PolicyName": arn.rsplit("/", 1)[1]}
                                     for arn in self.attached]}

    def detach_role_policy(self, RoleName, PolicyArn):
        self.attached.remove(PolicyArn)

    def delete_policy(self, PolicyArn):
        del self.policies[PolicyArn]

    def delete_role(self, RoleName):
        self.role = None


class FakeSession:
    def __init__(self, iam):
        self.iam = iam

    def client(self, name, config=None):
        return self.iam




def statements(policy):
    return policy["Statement"]


def allowed_actions(policy):
    return {action for statement in statements(policy) if statement["Effect"] == "Allow"
            for action in statement["Action"]}


def test_the_aws_policy_leaves_out_every_read_of_a_secret_a_log_or_a_file():
    policy = deploy_aws.grant_policy(ACCOUNT, "us-east-2")
    assert not GRANT_LEFT_OUT & allowed_actions(policy)
    assert "secretsmanager:PutSecretValue" in allowed_actions(policy)
    assert "batch:RegisterJobDefinition" in allowed_actions(policy)


def test_the_aws_policy_names_resources_and_no_wildcard_action():
    policy = deploy_aws.grant_policy(ACCOUNT, "us-east-2")
    for statement in statements(policy):
        if statement["Effect"] != "Allow":
            continue
        assert all(not action.endswith("*") for action in statement["Action"])
        for resource in statement["Resource"]:
            assert resource == "*" or "pdt" in resource or "service-role" in resource \
                or ":vpc/" in resource or ":security-group/" in resource or ":job/" in resource, \
                resource


def test_each_new_role_needs_the_boundary():
    policy = deploy_aws.grant_policy(ACCOUNT, "us-east-2")
    bounded = next(item for item in statements(policy) if "iam:CreateRole" in item["Action"])
    assert bounded["Condition"]["StringEquals"]["iam:PermissionsBoundary"] == (
        f"arn:aws:iam::{ACCOUNT}:policy/{GRANT_BOUNDARY}")
    assert "iam:PutRolePolicy" in bounded["Action"]
    others = [item for item in statements(policy)
              if item["Effect"] == "Allow" and item is not bounded]
    assert not any("iam:CreateRole" in item["Action"] or "iam:PutRolePolicy" in item["Action"]
                   for item in others)


@pytest.mark.parametrize("document", [
    lambda: deploy_aws.grant_policy(ACCOUNT, "us-east-2"), lambda: deploy_aws.grant_boundary(ACCOUNT)])
def test_no_action_changes_the_identity_or_the_boundary(document):
    denies = [item for item in statements(document()) if item["Effect"] == "Deny"]
    resources = {resource for item in denies for resource in item["Resource"]}
    assert f"arn:aws:iam::{ACCOUNT}:role/{GRANT_ROLE}" in resources
    assert f"arn:aws:iam::{ACCOUNT}:policy/{GRANT_ROLE}*" in resources
    assert any(item["Action"] == ["iam:DeleteRolePermissionsBoundary"] for item in denies)


def test_the_boundary_holds_what_each_job_role_needs_and_opens_no_more():
    boundary = deploy_aws.grant_boundary(ACCOUNT)
    actions = allowed_actions(boundary)
    assert {"ecr:BatchGetImage", "logs:PutLogEvents", "secretsmanager:GetSecretValue",
            "s3:PutObject", "batch:SubmitJob", "iam:PassRole"} <= actions
    assert not any(action.startswith("iam:") and action != "iam:PassRole" for action in actions)


def test_each_managed_policy_fits_the_aws_limit():
    parts = deploy_aws.grant_policy_parts(ACCOUNT, "us-east-2")
    for part in parts:
        assert deploy_aws.policy_size(part["Statement"]) <= deploy_aws.GRANT_POLICY_LIMIT
    together = {action for part in parts for action in allowed_actions(part)}
    assert together == allowed_actions(deploy_aws.grant_policy(ACCOUNT, "us-east-2"))



@pytest.fixture
def keep(monkeypatch):
    monkeypatch.setenv(deploy_common.KEEP_SECRET_ENV, "1")


def test_a_worker_deploy_gathers_no_secret(keep, monkeypatch, tmp_path):
    monkeypatch.setenv("TOKEN", "value")
    app = {"name": "hr", "dir": tmp_path, "env": {"required": ["TOKEN", "MISSING"]}, "platform": {}}
    assert deploy_common.gather_secrets(app) == {}


def test_a_worker_deploy_adds_no_registration_and_reads_the_versions(keep):
    current = json.dumps({"TOKEN": "value", "PDT_INSTALLATION_ID": "ins_1"})
    assert deploy_common.registration_secrets(current) == {}
    assert deploy_common.reads_registration() is True


def test_a_normal_deploy_is_unchanged(monkeypatch):
    monkeypatch.delenv(deploy_common.KEEP_SECRET_ENV, raising=False)
    assert deploy_common.keeps_secret() is False


class FakeSecrets:
    def __init__(self):
        self.calls = []

    def describe_secret(self, SecretId):
        return {"ARN": "arn:secret", "Tags": [{"Key": "managed-by", "Value": "pdt"}]}

    def __getattr__(self, name):
        def call(**kwargs):
            self.calls.append(name)
            return {"SecretString": "{}", "ARN": "new"}
        return call


def test_a_worker_deploy_neither_reads_nor_writes_the_aws_secret(keep):
    secrets = FakeSecrets()
    assert deploy_aws.ensure_secret(secrets, "pdt-hr", "{}") == "arn:secret"
    assert secrets.calls == []


def test_a_normal_aws_deploy_still_writes_the_secret(monkeypatch):
    monkeypatch.delenv(deploy_common.KEEP_SECRET_ENV, raising=False)
    secrets = FakeSecrets()
    deploy_aws.ensure_secret(secrets, "pdt-hr", '{"A": "1"}')
    assert "put_secret_value" in secrets.calls


def test_a_worker_deploy_lists_the_azure_versions_and_opens_no_value(keep, monkeypatch):
    monkeypatch.setattr(deploy_azure, "arm_get", lambda token, url, *_a, **_k: (
        {"id": "vault"} if "vaults" in url else pytest.fail("read the value")))
    monkeypatch.setattr(deploy_azure, "secret_versions", lambda *_a: [{"id": "v1"}])
    settings = {"token": "t", "vault": "pdtvault", "subscription": "s", "resource_group": "pdt"}
    vault, current, versions = deploy_azure.secret_reads(settings, "vt", "pdt-hr", True)
    assert current is None and versions == [{"id": "v1"}]


def test_a_worker_role_gets_the_boundary_new_and_existing(monkeypatch):
    monkeypatch.setenv(deploy_aws.BOUNDARY_ENV, "arn:boundary")
    iam = FakeIam()
    deploy_aws.ensure_role(iam, "pdt-hr", "ecs-tasks.amazonaws.com", "pdt-job", [])
    created = next(kwargs for name, kwargs in iam.calls if name == "create_role")
    assert created["PermissionsBoundary"] == "arn:boundary"
    iam.role = {"Arn": "x", "Tags": [{"Key": "managed-by", "Value": "pdt"}]}
    iam.calls.clear()
    iam.list_role_policies = lambda RoleName: {"PolicyNames": []}
    deploy_aws.ensure_role(iam, "pdt-hr", "ecs-tasks.amazonaws.com", "pdt-job", [])
    assert ("put_role_permissions_boundary",
            {"RoleName": "pdt-hr", "PermissionsBoundary": "arn:boundary"}) in iam.calls


def test_a_normal_role_gets_no_boundary(monkeypatch):
    monkeypatch.delenv(deploy_aws.BOUNDARY_ENV, raising=False)
    iam = FakeIam()
    deploy_aws.ensure_role(iam, "pdt-hr", "ecs-tasks.amazonaws.com", "pdt-job", [])
    created = next(kwargs for name, kwargs in iam.calls if name == "create_role")
    assert "PermissionsBoundary" not in created


def test_a_worker_login_skips_the_profile_and_the_simulation(monkeypatch):
    monkeypatch.setenv(deploy_aws.BOUNDARY_ENV, "arn:boundary")

    class Sts:
        def get_caller_identity(self):
            return {"Account": ACCOUNT, "Arn": f"arn:aws:sts::{ACCOUNT}:assumed-role/pdt-autoidm/w"}

    account, wait = deploy_aws.preflight(Sts(), None, ACCOUNT, "us-east-2", ["iam:GetRole"])
    assert account == ACCOUNT
    assert wait() is None


def test_the_root_deploy_skips_the_env_check_for_a_worker(keep, monkeypatch, tmp_path):
    monkeypatch.setenv(deploy.config.LOCK_PROJECT_ENV, str(tmp_path))
    monkeypatch.setattr(deploy, "_load", lambda _name: (
        {"dir": tmp_path, "name": "hr", "schedule": "daily", "platform": {"provider": "aws"}}, "aws"))
    monkeypatch.setattr(deploy.config, "validate_app", lambda _name: [])
    monkeypatch.setattr(deploy.config, "env_file_lines", lambda _start: [])
    monkeypatch.setattr(deploy.config, "load_env", lambda _start: [])
    monkeypatch.setattr(deploy.config, "missing_env", lambda _app: pytest.fail("checked the env"))
    monkeypatch.setattr(deploy.config, "uses_email", lambda _app: False)
    monkeypatch.setattr(deploy.config, "powershell_scripts", lambda _dir: [])
    monkeypatch.setattr(deploy.config, "found_env_lines", lambda _app: [])
    monkeypatch.setattr(deploy, "choose_schedule", lambda *_a: "")
    monkeypatch.setattr(deploy.regions, "choose_region", lambda *_a: "")
    monkeypatch.setattr(deploy, "leave_old_place", lambda *_a: 0)
    monkeypatch.setattr(deploy.alerts_cli, "offer", lambda *_a: None)
    monkeypatch.setattr(deploy, "provider_step", lambda *_a: 0)
    monkeypatch.setattr(deploy, "deployed_next_steps", lambda *_a, **_k: None)
    assert deploy._deploy("hr", True, False, False, False) == 0


def test_a_normal_root_deploy_still_checks_the_env(monkeypatch, tmp_path, capsys):
    monkeypatch.delenv(deploy_common.KEEP_SECRET_ENV, raising=False)
    monkeypatch.setattr(deploy, "_load", lambda _name: (
        {"dir": tmp_path, "name": "hr", "schedule": "daily", "platform": {"provider": "aws"}}, "aws"))
    monkeypatch.setattr(deploy.config, "validate_app", lambda _name: [])
    monkeypatch.setattr(deploy.config, "env_file_lines", lambda _start: [])
    monkeypatch.setattr(deploy.config, "load_env", lambda _start: [])
    monkeypatch.setattr(deploy.config, "missing_env", lambda _app: "TOKEN is not set")
    monkeypatch.setattr(deploy.config, "uses_email", lambda _app: False)
    monkeypatch.setattr(deploy.config, "powershell_scripts", lambda _dir: [])
    assert deploy._deploy("hr", True, False, False, False) == 1
    assert "TOKEN is not set" in capsys.readouterr().out


def test_places_of_a_project_come_from_the_config_with_no_login(tmp_path, monkeypatch):
    project = tmp_path / "example"
    (project / "hr").mkdir(parents=True)
    (project / "pdt.yml").write_text(
        f"platform:\n  provider: aws\n  account: '{ACCOUNT}'\n  region: us-east-2\n")
    (project / "hr" / "config.yml").write_text("schedule: daily\n")
    (project / "hr" / "run.py").write_text("print('hi')\n")
    monkeypatch.chdir(project)
    monkeypatch.delenv("PDT_PROJECT", raising=False)
    assert grants.places_of_project() == [AWS_PLACE]


def test_a_project_with_a_place_not_yet_known_lists_none(tmp_path, monkeypatch):
    project = tmp_path / "example"
    (project / "hr").mkdir(parents=True)
    (project / "pdt.yml").write_text("platform:\n  provider: aws\n  region: us-east-2\n")
    (project / "hr" / "config.yml").write_text("schedule: daily\n")
    (project / "hr" / "run.py").write_text("print('hi')\n")
    monkeypatch.chdir(project)
    monkeypatch.delenv("PDT_PROJECT", raising=False)
    assert grants.places_of_project() == []


def test_aws_credentials_become_the_env_of_the_child_with_the_boundary():
    answer = {"provider": "aws", "expires": "x", "aws": {
        "access_key_id": "AKIA", "secret_access_key": "secret", "session_token": "token",
        "region": "us-east-2"}}
    env = grants.credentials(AWS_PLACE, answer)
    assert env["AWS_ACCESS_KEY_ID"] == "AKIA" and env["AWS_SESSION_TOKEN"] == "token"
    assert env["AWS_REGION"] == env["AWS_DEFAULT_REGION"] == "us-east-2"
    assert env[grants.BOUNDARY_ENV] == f"arn:aws:iam::{ACCOUNT}:policy/{GRANT_BOUNDARY}"


def test_credentials_of_another_provider_are_refused():
    with pytest.raises(RuntimeError, match="no aws credentials"):
        grants.credentials(AWS_PLACE, {"provider": "azure", "azure": {}})


def test_azure_credentials_log_in_with_the_federated_token_in_a_memory_folder(monkeypatch,
                                                                                tmp_path):
    monkeypatch.setattr(grants, "MEMORY_FOLDER", tmp_path)
    seen = {}

    def run(args, env, **_kwargs):
        seen["args"], seen["env"] = args, env
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(grants.subprocess, "run", run)
    env = grants.credentials(AZURE_PLACE, {"provider": "azure", "azure": {
        "tenant": "tenant-1", "client_id": "client-1", "federated_token": "jwt",
        "subscription": "sub"}})
    assert seen["args"][:2] == ["az", "login"]
    assert "--federated-token" in seen["args"] and "jwt" in seen["args"]
    assert env["AZURE_CONFIG_DIR"] == seen["env"]["AZURE_CONFIG_DIR"]
    assert env[grants.GRANT_FOLDER_ENV] == env["AZURE_CONFIG_DIR"]
    assert env["AZURE_CONFIG_DIR"].startswith(str(tmp_path))


def test_a_refused_azure_login_stops_the_command(monkeypatch, tmp_path):
    monkeypatch.setattr(grants, "MEMORY_FOLDER", tmp_path)
    monkeypatch.setattr(grants.subprocess, "run", lambda args, **_k: subprocess.CompletedProcess(
        args, 1, "", "AADSTS700024"))
    with pytest.raises(RuntimeError, match="AADSTS700024"):
        grants.credentials(AZURE_PLACE, {"provider": "azure", "azure": {
            "tenant": "t", "client_id": "c", "federated_token": "j", "subscription": "s"}})


def test_google_credentials_are_a_token_file_in_a_memory_folder(monkeypatch, tmp_path):
    monkeypatch.setattr(grants, "MEMORY_FOLDER", tmp_path)
    env = grants.credentials(GOOGLE_PLACE, {"provider": "google-cloud", "google-cloud": {
        "access_token": "ya29", "project": "my-project-1"}})
    assert open(env["CLOUDSDK_AUTH_ACCESS_TOKEN_FILE"]).read() == "ya29"
    assert env["CLOUDSDK_CORE_PROJECT"] == "my-project-1"
    assert env["CLOUDSDK_CONFIG"] == env[grants.GRANT_FOLDER_ENV]


def test_a_runner_with_no_memory_folder_stops(monkeypatch, tmp_path):
    monkeypatch.setattr(grants, "MEMORY_FOLDER", tmp_path / "none")
    with pytest.raises(RuntimeError, match="memory"):
        grants.credentials(GOOGLE_PLACE, {"provider": "google-cloud", "google-cloud": {
            "access_token": "ya29", "project": "p"}})
