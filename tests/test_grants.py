import ast
import json
import subprocess
from pathlib import Path

import pytest
from botocore.exceptions import ClientError

from pdt import deploy, deploy_aws, deploy_azure, deploy_common, deploy_google_cloud, grants
from pdt.deploy import DECLINED
from pdt.deploy_aws import GRANT_BOUNDARY, GRANT_LEFT_OUT, GRANT_ROLE

ACCOUNT = "123456789012"
AWS_PLACE = f"aws:{ACCOUNT}:us-east-2"
AZURE_PLACE = "azure:00000000-0000-0000-0000-000000000001:eastus2"
GOOGLE_PLACE = "google-cloud:my-project-1:us-central1"
VALUES = {"external_id": "b" * 32,
          "principal": {"aws": "arn:aws:iam::999999999999:role/autoidm",
                        "azure": {"tenant": "t", "client_id": "client-1"},
                        "google-cloud": "autoidm@autoidm.iam.gserviceaccount.com"}}
MODULES = [(AWS_PLACE, deploy_aws), (AZURE_PLACE, deploy_azure), (GOOGLE_PLACE, deploy_google_cloud)]


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


@pytest.fixture
def iam(monkeypatch):
    found = FakeIam()
    monkeypatch.setattr(deploy_aws, "grant_login", lambda _place: FakeSession(found))
    return found


def no_network(*_args, **_kwargs):
    pytest.fail("a plan signs in to nothing and reads nothing")


@pytest.mark.parametrize("place, module", MODULES)
def test_a_plan_prints_the_resources_and_touches_nothing(place, module, monkeypatch, capsys):
    monkeypatch.setattr(module, "grant_login", no_network)
    monkeypatch.setattr(deploy, "proceed", lambda _yes: pytest.fail("a plan asks nothing"))
    monkeypatch.setattr(subprocess, "run", no_network)
    assert grants.run_verb("grant", place, False, True, module.GRANT) == 0
    out = capsys.readouterr().out
    assert "Plan:" in out and "pdt-autoidm" in out and "managed-by=pdt" in out


@pytest.mark.parametrize("place, module", MODULES)
def test_a_remove_plan_touches_nothing(place, module, monkeypatch, capsys):
    monkeypatch.setattr(module, "grant_login", no_network)
    assert grants.run_verb("grant-remove", place, False, True, module.GRANT) == 0
    assert "pdt-autoidm" in capsys.readouterr().out


@pytest.mark.parametrize("place, module", MODULES)
def test_a_no_to_the_question_creates_nothing(place, module, monkeypatch, capsys):
    monkeypatch.setattr(module, "grant_login", no_network)
    monkeypatch.setattr(deploy, "can_prompt", lambda _interactive: True)
    monkeypatch.setattr(deploy.console, "confirm", lambda *_a: False)
    monkeypatch.setenv(grants.GRANT_ENV, json.dumps(VALUES))
    assert grants.run_verb("grant", place, False, False, module.GRANT) == DECLINED
    assert "Plan:" in capsys.readouterr().out


def test_every_provider_script_takes_the_grant_verbs_in_its_main():
    source = Path(deploy.__file__).parent
    for name in deploy.PROVIDERS.values():
        text = (source / name).read_text(encoding="utf-8")
        assert "*grants.COMMANDS" in text, name
        assert "if args.command in grants.COMMANDS" in text, name


def test_the_provider_neutral_code_has_no_provider_branch():
    for module in (grants,):
        for node in ast.walk(ast.parse(Path(module.__file__).read_text(encoding="utf-8"))):
            if isinstance(node, ast.Compare):
                for side in (node.left, *node.comparators):
                    assert not (isinstance(side, ast.Constant)
                                and side.value in ("aws", "azure", "google-cloud", "windows"))


def test_windows_has_no_grant(capsys):
    assert grants.no_grant("grant", "x") == 0
    assert "no grant" in capsys.readouterr().out
    assert grants.no_grant("grant-env", "x") == 1


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
                or ":vpc/" in resource or ":security-group/" in resource, resource


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


def test_the_aws_grant_creates_the_role_the_boundary_and_the_policy_with_tags(iam):
    role = deploy_aws.grant_create(AWS_PLACE, VALUES, True)
    assert role == f"arn:aws:iam::{ACCOUNT}:role/{GRANT_ROLE}"
    names = [kwargs["PolicyName"] for name, kwargs in iam.calls if name == "create_policy"]
    assert names[0] == GRANT_BOUNDARY
    assert names[1].startswith(f"{GRANT_ROLE}-us-east-2")
    for name, kwargs in iam.calls:
        if name in ("create_policy", "create_role"):
            assert {"Key": "managed-by", "Value": "pdt"} in kwargs["Tags"]
    created = next(kwargs for name, kwargs in iam.calls if name == "create_role")
    assert created["MaxSessionDuration"] == 3600
    trust = json.loads(created["AssumeRolePolicyDocument"])["Statement"][0]
    assert trust["Principal"]["AWS"] == VALUES["principal"]["aws"]
    assert trust["Condition"]["StringEquals"]["sts:ExternalId"] == "b" * 32
    assert len(iam.attached) == len(names) - 1


def test_a_second_aws_grant_updates_and_adds_its_region(iam):
    deploy_aws.grant_create(AWS_PLACE, VALUES, True)
    first = list(iam.attached)
    deploy_aws.grant_create(f"aws:{ACCOUNT}:eu-west-1", VALUES, True)
    assert any("eu-west-1" in arn for arn in iam.attached)
    assert all(arn in iam.attached for arn in first)
    assert any(name == "update_assume_role_policy" for name, _kwargs in iam.calls)
    assert sum(name == "create_role" for name, _kwargs in iam.calls) == 1


def test_an_aws_role_pdt_did_not_make_stops_the_grant(iam):
    iam.role = {"Arn": "x", "Tags": []}
    with pytest.raises(SystemExit):
        deploy_aws.grant_create(AWS_PLACE, VALUES, True)


def test_the_aws_grant_needs_the_principal_and_the_external_id(iam):
    with pytest.raises(SystemExit):
        deploy_aws.grant_create(AWS_PLACE, {}, True)


def test_removing_one_aws_region_keeps_the_other_and_the_last_removes_the_identity(iam):
    deploy_aws.grant_create(AWS_PLACE, VALUES, True)
    deploy_aws.grant_create(f"aws:{ACCOUNT}:eu-west-1", VALUES, True)
    deploy_aws.grant_remove(AWS_PLACE, {}, True)
    assert iam.role is not None
    assert all("us-east-2" not in arn for arn in iam.attached)
    deploy_aws.grant_remove(f"aws:{ACCOUNT}:eu-west-1", {}, True)
    assert iam.role is None
    assert iam.policies == {}


def test_the_aws_worker_env_goes_through_the_broker_then_the_role_with_the_external_id(
        monkeypatch):
    seen = []

    class Sts:
        def __init__(self, **kwargs):
            self.login = kwargs.get("aws_session_token")

        def assume_role(self, **kwargs):
            seen.append((self.login, kwargs))
            return {"Credentials": {"AccessKeyId": "a", "SecretAccessKey": "s",
                                    "SessionToken": f"t{len(seen)}"}}

    monkeypatch.setattr(deploy_aws.boto3, "client", lambda *_a, **kwargs: Sts(**kwargs))
    role = f"arn:aws:iam::{ACCOUNT}:role/{GRANT_ROLE}"
    broker = "arn:aws:iam::999999999999:role/pdt-backend-grant-broker"
    env = deploy_aws.grant_env(AWS_PLACE, {"role": role, "external_id": "b" * 32,
                                           "principal": {"aws": broker}})
    assert seen[0] == (None, {"RoleArn": broker, "RoleSessionName": "pdt-worker"})
    login, second = seen[1]
    assert login == "t1" and second["RoleArn"] == role and second["ExternalId"] == "b" * 32
    assert second["DurationSeconds"] == 3600
    assert env["AWS_SECRET_ACCESS_KEY"] == "s" and env["AWS_SESSION_TOKEN"] == "t2"
    assert env["AWS_REGION"] == "us-east-2"
    assert env[deploy_aws.BOUNDARY_ENV].endswith(f"policy/{GRANT_BOUNDARY}")


def test_the_aws_worker_env_refuses_a_role_whose_trust_holds_another_external_id(monkeypatch):
    from botocore.exceptions import ClientError

    class Sts:
        def __init__(self, **kwargs):
            self.second = "aws_session_token" in kwargs

        def assume_role(self, **_kwargs):
            if self.second:
                raise ClientError({"Error": {"Code": "AccessDenied", "Message": "no"}}, "AssumeRole")
            return {"Credentials": {"AccessKeyId": "a", "SecretAccessKey": "s", "SessionToken": "t"}}

    monkeypatch.setattr(deploy_aws.boto3, "client", lambda *_a, **kwargs: Sts(**kwargs))
    with pytest.raises(grants.Mismatch, match="external ID"):
        deploy_aws.grant_env(AWS_PLACE, {"role": f"arn:aws:iam::{ACCOUNT}:role/{GRANT_ROLE}",
                                         "external_id": "b" * 32, "principal": {"aws": "broker"}})


def test_the_aws_worker_env_refuses_a_role_in_another_account():
    with pytest.raises(SystemExit):
        deploy_aws.grant_env(AWS_PLACE, {"role": "arn:aws:iam::111111111111:role/x"})


class AzureCalls:
    def __init__(self, monkeypatch):
        self.reads, self.writes, self.assigned, self.revoked = [], [], [], []
        self.missing = {("ad", "sp", "show"), ("group", "show")}
        monkeypatch.setattr(deploy_azure, "az_json", self.az_json)
        monkeypatch.setattr(deploy_azure, "run_quiet", lambda *args, **_k: self.writes.append(args) or "")
        monkeypatch.setattr(deploy_azure, "assign_role", lambda scope, principal, role, condition="":
                            self.assigned.append((scope, role, condition)))
        monkeypatch.setattr(deploy_azure, "revoke_role", lambda scope, principal, role:
                            self.revoked.append((scope, role)))

    def az_json(self, *args):
        self.reads.append(args)
        if args[:2] == ("account", "show"):
            return {"tenantId": "tenant-1"}
        if args[:3] in self.missing or args[:2] in self.missing:
            return None
        return {"id": "object-1"}


def test_the_azure_grant_scopes_every_role_to_the_pdt_groups(monkeypatch):
    calls = AzureCalls(monkeypatch)
    role = deploy_azure.grant_create(AZURE_PLACE, VALUES, True)
    assert role == "object-1"
    assert ("ad", "sp", "create", "--id", "client-1") in calls.reads
    assert ("ad", "sp", "update", "--id", "object-1", "--add", "tags", "managed-by=pdt") in calls.writes
    created = [args for args in calls.writes if args[:2] == ("group", "create")]
    assert [args[3] for args in created] == ["pdt", "pdt-shared"]
    assert all(args[-2:] == ("--tags", "managed-by=pdt") for args in created)
    scopes = {scope for scope, _role, _condition in calls.assigned}
    assert scopes == {"/subscriptions/00000000-0000-0000-0000-000000000001/resourceGroups/pdt",
                      "/subscriptions/00000000-0000-0000-0000-000000000001/resourceGroups/pdt-shared"}
    roles = {name for _scope, name, _condition in calls.assigned}
    assert roles == {"Contributor", "Key Vault Reader", "Role Based Access Control Administrator"}
    assert "Key Vault Secrets Officer" not in roles


def test_the_azure_role_writer_may_assign_only_the_named_roles(monkeypatch):
    calls = AzureCalls(monkeypatch)
    deploy_azure.grant_create(AZURE_PLACE, VALUES, True)
    conditions = [condition for _scope, name, condition in calls.assigned
                  if name == "Role Based Access Control Administrator"]
    assert all(deploy_azure.ROLE_IDS["AcrPull"] in condition for condition in conditions)
    assert all(deploy_azure.ROLE_IDS["Contributor"] not in condition for condition in conditions)
    assert conditions and all(
        deploy_azure.ROLE_IDS["Role Based Access Control Administrator"] not in condition
        for condition in conditions)


def test_the_azure_remove_deletes_the_identity(monkeypatch):
    calls = AzureCalls(monkeypatch)
    calls.missing = set()
    deploy_azure.grant_remove(AZURE_PLACE, {"role": "object-1"}, True)
    assert ("ad", "sp", "delete", "--id", "object-1") in calls.writes
    assert len(calls.revoked) == 6


def azure_worker(monkeypatch, tmp_path, tags):
    monkeypatch.setattr(grants, "MEMORY_FOLDER", tmp_path)
    seen = []

    def run(args, env=None, **_kwargs):
        seen.append((args, env))
        if "group" in args:
            return subprocess.CompletedProcess(args, 0, json.dumps(tags), "")
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(deploy_azure.subprocess, "run", run)
    monkeypatch.setattr(deploy_azure, "subscription_tenant", lambda _subscription: "tenant-1")
    return seen


def test_the_azure_worker_env_keeps_the_login_in_a_memory_folder(monkeypatch, tmp_path):
    seen = azure_worker(monkeypatch, tmp_path, {grants.EXTERNAL_ID_TAG: "e" * 32})
    env = deploy_azure.grant_env(AZURE_PLACE, {"role": "object-1", "external_id": "e" * 32,
                                               "principal": VALUES["principal"],
                                               "credential": "client-secret"})
    login, login_env = seen[0]
    assert "--service-principal" in login
    assert login[login.index("--client-secret") + 1] == "client-secret"
    assert login[login.index("--tenant") + 1] == "tenant-1"
    assert login[login.index("--username") + 1] == "client-1"
    assert env["AZURE_CONFIG_DIR"] == login_env["AZURE_CONFIG_DIR"]
    assert Path(env["AZURE_CONFIG_DIR"]).parent == tmp_path
    assert env["PDT_GRANT_FOLDER"] == env["AZURE_CONFIG_DIR"]


def test_the_azure_worker_env_refuses_a_group_with_another_external_id(monkeypatch, tmp_path):
    azure_worker(monkeypatch, tmp_path, {grants.EXTERNAL_ID_TAG: "other"})
    with pytest.raises(grants.Mismatch, match="autoidm-external-id=other"):
        deploy_azure.grant_env(AZURE_PLACE, {"role": "object-1", "external_id": "e" * 32,
                                             "principal": VALUES["principal"],
                                             "credential": "client-secret"})


def test_a_worker_with_no_memory_folder_stops(monkeypatch, tmp_path):
    monkeypatch.setattr(grants, "MEMORY_FOLDER", tmp_path / "none")
    with pytest.raises(RuntimeError, match="in memory"):
        grants.memory_folder("x")


def google_calls(monkeypatch, known=()):
    writes = []
    monkeypatch.setattr(deploy_google_cloud, "run_quiet", lambda *args, **_k: writes.append(args) or "")
    monkeypatch.setattr(deploy_google_cloud, "preflight", lambda *_a: "my-project-1")
    monkeypatch.setattr(deploy_google_cloud, "service_account_or_none",
                        lambda _project, email: {"email": email} if email in known else None)
    return writes


def test_the_google_grant_has_named_roles_and_no_project_iam_write(monkeypatch):
    writes = google_calls(monkeypatch)
    role = deploy_google_cloud.grant_create(GOOGLE_PLACE, VALUES, True)
    assert role == "pdt-autoidm@my-project-1.iam.gserviceaccount.com"
    created = next(args for args in writes if args[:3] == ("iam", "service-accounts", "create"))
    assert created[3] == "pdt-autoidm"
    assert created[-1] == f"managed-by=pdt {grants.EXTERNAL_ID_TAG}={VALUES['external_id']}"
    token = next(args for args in writes if "roles/iam.serviceAccountTokenCreator" in args)
    assert f"serviceAccount:{VALUES['principal']['google-cloud']}" in token
    project_roles = [args[args.index("--role") + 1] for args in writes
                     if args[:2] == ("projects", "add-iam-policy-binding")]
    assert project_roles == list(deploy_google_cloud.GRANT_ROLES)
    for name in project_roles:
        assert name not in ("roles/owner", "roles/editor", "roles/resourcemanager.projectIamAdmin",
                            "roles/iam.securityAdmin", "roles/secretmanager.secretAccessor")


def test_the_google_remove_deletes_the_identity(monkeypatch):
    writes = google_calls(monkeypatch, known=("pdt-autoidm@my-project-1.iam.gserviceaccount.com",))
    deploy_google_cloud.grant_remove(GOOGLE_PLACE, {}, True)
    assert writes[-1][:3] == ("iam", "service-accounts", "delete")


def google_worker(monkeypatch, tmp_path, description):
    from google.oauth2 import service_account
    monkeypatch.setattr(grants, "MEMORY_FOLDER", tmp_path)

    class Own:
        token = "own-token"

        def refresh(self, _request):
            pass

    monkeypatch.setattr(service_account.Credentials, "from_service_account_info",
                        lambda info, scopes: Own())
    seen = []

    def google_json(url, token, body=None):
        seen.append((url, token, body))
        if url.endswith(":generateAccessToken"):
            return {"accessToken": "role-token"}
        return {"description": description}

    monkeypatch.setattr(deploy_google_cloud, "google_json", google_json)
    return seen


def test_the_google_worker_env_is_a_token_file_for_the_role(monkeypatch, tmp_path):
    seen = google_worker(monkeypatch, tmp_path, f"managed-by=pdt {grants.EXTERNAL_ID_TAG}=e")
    env = deploy_google_cloud.grant_env(GOOGLE_PLACE, {"role": "pdt-autoidm@x", "external_id": "e",
                                                       "credential": "{}"})
    assert seen[0][0].endswith("/serviceAccounts/pdt-autoidm@x:generateAccessToken")
    assert seen[0][1] == "own-token" and seen[1][1] == "role-token"
    assert Path(env["CLOUDSDK_AUTH_ACCESS_TOKEN_FILE"]).read_text() == "role-token"
    assert env["CLOUDSDK_CORE_PROJECT"] == "my-project-1"
    assert Path(env["CLOUDSDK_CONFIG"]).parent == tmp_path
    assert "GOOGLE_APPLICATION_CREDENTIALS" not in env


def test_the_google_worker_env_refuses_another_external_id(monkeypatch, tmp_path):
    google_worker(monkeypatch, tmp_path, "managed-by=pdt")
    with pytest.raises(grants.Mismatch, match="does not hold"):
        deploy_google_cloud.grant_env(GOOGLE_PLACE, {"role": "pdt-autoidm@x", "external_id": "e",
                                                     "credential": "{}"})


def grant_values_answer(found, path, body, **changes):
    return {"role": "r", "external_id": f"from {path} {body['place']}",
            "principal": VALUES["principal"], "credential_parameter": None, **changes}


def test_the_worker_gets_the_grant_values_from_autoidm_and_credentials_through_a_pipe(
        monkeypatch):
    from pdt.utils import autoidm
    seen = []

    def run(args, env=None, **kwargs):
        seen.append((args, env, kwargs))
        if grants.WORKER_PARAMETER in args:
            return subprocess.CompletedProcess(args, 0, stdout='{"value": "the-secret"}', stderr="")
        return subprocess.CompletedProcess(args, 0, stdout='{"AWS_SESSION_TOKEN": "t"}', stderr="")

    monkeypatch.setattr(grants.subprocess, "run", run)
    monkeypatch.setattr(deploy, "announce_install", lambda _script: None)
    monkeypatch.setattr(autoidm, "post", lambda found, path, body: grant_values_answer(
        found, path, body, credential_parameter="/pdt-backend/grant-identities/c/azure"))
    found = grants.credentials({"place": AWS_PLACE, "role": "r"}, object())
    assert found == {"AWS_SESSION_TOKEN": "t"}
    assert seen[0][0][-2:] == [grants.WORKER_PARAMETER, "/pdt-backend/grant-identities/c/azure"]
    args, env, kwargs = seen[1]
    assert "grant-env" in args and kwargs["capture_output"] is True
    values = json.loads(env[grants.GRANT_ENV])
    assert values["role"] == "r" and values["credential"] == "the-secret"
    assert values["external_id"] == f"from /v1/grants/values {AWS_PLACE}"


def test_a_grant_env_mismatch_refuses_the_command(monkeypatch):
    from pdt.service_commands import Refused
    from pdt.utils import autoidm
    monkeypatch.setattr(grants.subprocess, "run", lambda args, **_k: subprocess.CompletedProcess(
        args, 0, stdout=json.dumps({grants.REFUSED_KEY: "another external ID"}), stderr=""))
    monkeypatch.setattr(deploy, "announce_install", lambda _script: None)
    monkeypatch.setattr(autoidm, "post", grant_values_answer)
    with pytest.raises(Refused, match="another external ID"):
        grants.credentials({"place": AWS_PLACE, "role": "r"}, object())


def test_a_failed_credentials_call_says_so(monkeypatch):
    from pdt.utils import autoidm
    monkeypatch.setattr(deploy, "announce_install", lambda _script: None)
    monkeypatch.setattr(autoidm, "post", grant_values_answer)
    monkeypatch.setattr(grants.subprocess, "run", lambda args, **_k: subprocess.CompletedProcess(
        args, 1, stdout="", stderr="AccessDenied"))
    with pytest.raises(RuntimeError, match="AccessDenied"):
        grants.credentials({"place": AWS_PLACE, "role": "r"}, object())


def test_azure_names_the_tenant_of_a_subscription_with_no_token(monkeypatch):
    import email.message
    import urllib.error
    headers = email.message.Message()
    headers["WWW-Authenticate"] = ('Bearer authorization_uri="https://login.microsoftonline.com/'
                                   '11111111-2222-3333-4444-555555555555"')

    def urlopen(url, timeout=None):
        raise urllib.error.HTTPError(url, 401, "Unauthorized", headers, None)

    monkeypatch.setattr(deploy_azure.urllib.request, "urlopen", urlopen)
    assert deploy_azure.subscription_tenant("sub") == "11111111-2222-3333-4444-555555555555"


def test_grant_add_starts_makes_and_finishes(monkeypatch, tmp_path, capsys):
    from service_fixtures import FakePortal

    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    from pdt.utils import autoidm
    monkeypatch.setenv(autoidm.DIR_ENV, str(tmp_path))
    portal = FakePortal()
    portal.routes["/v1/grants/start"] = lambda _body: (200, VALUES)
    portal.routes["/v1/grants/finish"] = lambda _body: (200, {"status": "pending"})
    found = autoidm.Registration("ins_1", portal.url, Ed25519PrivateKey.generate(), "x")
    seen = {}

    def provider_run(command, place, assume_yes, values, stdout=None, plan_only=False):
        seen.update(command=command, values=values, stdout=stdout)
        return subprocess.CompletedProcess([], 0, stdout='{"role": "the-role"}')

    monkeypatch.setattr(grants, "provider_run", provider_run)
    assert grants.add(AWS_PLACE, True, False, found) == 0
    assert seen["command"] == "grant" and seen["values"]["external_id"] == "b" * 32
    assert portal.bodies("/v1/grants/finish") == [{"version": 1, "place": AWS_PLACE, "role": "the-role"}]
    assert grants.read_records()[AWS_PLACE]["role"] == "the-role"
    assert grants.show(True) == 0
    assert json.loads(capsys.readouterr().out.splitlines()[-1])[0]["place"] == AWS_PLACE
    portal.close()


def test_grant_add_with_a_bad_place_says_the_form(capsys):
    assert grants.add("nonsense", True, False) == 1
    assert "aws:123456789012:us-east-2" in capsys.readouterr().out


def test_a_grant_plan_sends_nothing_to_autoidm(monkeypatch):
    seen = {}

    def provider_run(command, place, assume_yes, values, stdout=None, plan_only=False):
        seen.update(plan_only=plan_only, values=values)
        return subprocess.CompletedProcess([], 0)

    monkeypatch.setattr(grants, "provider_run", provider_run)
    assert grants.add(AWS_PLACE, False, True) == 0
    assert seen == {"plan_only": True, "values": {}}


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
