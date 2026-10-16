"""A deploy to AWS builds the image with CodeBuild when Docker on this computer cannot."""

import zipfile
from io import BytesIO
from types import SimpleNamespace

import pytest
import yaml
from botocore.exceptions import ClientError

from conftest import add_app, plain
from pdt import deploy_aws_batch as batch_deploy
from pdt.deploy_aws import deployer_policy
from pdt.deploy_common import BUILD_TAG, REBUILD_ENV
from pdt.deploy_aws_batch import (
    BUILD_LOG_GROUP, BUILD_PROJECT, BUILD_ROLE, BUILDSPEC, DEPLOYER_ACTIONS, REPOSITORY,
    build_bucket,
)

ACCOUNT = "123456789012"
REGION = "us-east-1"
BUCKET = build_bucket(ACCOUNT, REGION)
REGISTRY = f"{ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com"
IMAGE = f"{REGISTRY}/{REPOSITORY}:my-app-latest"
MANAGED = [{"Key": "managed-by", "Value": "pdt"}]


def client_error(code: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": ""}}, "Call")


class FakeS3:
    def __init__(self, present=False):
        self.buckets = {BUCKET: {}} if present else {}
        self.tags = {BUCKET: MANAGED} if present else {}
        self.calls = []
        self.uploaded = {}

    def head_bucket(self, Bucket):
        if Bucket not in self.buckets:
            raise client_error("404")

    def create_bucket(self, Bucket, **kwargs):
        self.calls.append("create_bucket")
        self.buckets[Bucket] = {}

    def put_public_access_block(self, Bucket, PublicAccessBlockConfiguration):
        self.calls.append("put_public_access_block")

    def put_bucket_tagging(self, Bucket, Tagging):
        self.tags[Bucket] = Tagging["TagSet"]

    def get_bucket_tagging(self, Bucket):
        return {"TagSet": self.tags.get(Bucket, [])}

    def put_bucket_lifecycle_configuration(self, Bucket, LifecycleConfiguration):
        self.lifecycle = LifecycleConfiguration["Rules"]

    def upload_file(self, filename, bucket, key):
        with open(filename, "rb") as file:
            self.buckets[bucket][key] = file.read()
        self.uploaded[key] = self.buckets[bucket][key]

    def delete_object(self, Bucket, Key):
        self.calls.append(("delete_object", Key))
        del self.buckets[Bucket][Key]

    def list_objects_v2(self, Bucket):
        return {"Contents": [{"Key": key} for key in self.buckets[Bucket]]}

    def delete_objects(self, Bucket, Delete):
        for item in Delete["Objects"]:
            del self.buckets[Bucket][item["Key"]]

    def delete_bucket(self, Bucket):
        assert not self.buckets[Bucket], "a bucket must be empty before it is deleted"
        del self.buckets[Bucket]


class FakeCodeBuild:
    def __init__(self, status="SUCCEEDED", project=None):
        self.project = project
        self.status = status
        self.started = []
        self.calls = []
        self.polls = 0

    def batch_get_projects(self, names):
        return {"projects": [self.project] if self.project else []}

    def create_project(self, **kwargs):
        self.calls.append("create_project")
        self.project = kwargs

    def update_project(self, **kwargs):
        self.calls.append("update_project")
        self.project = kwargs

    def delete_project(self, name):
        self.calls.append("delete_project")
        self.project = None

    def start_build(self, **kwargs):
        self.started.append(kwargs)
        return {"build": {"id": "pdt:1"}}

    def batch_get_builds(self, ids):
        self.polls += 1
        logs = {"groupName": BUILD_LOG_GROUP, "streamName": "1"}
        if self.polls == 1:
            return {"builds": [{"id": "pdt:1", "buildComplete": False, "logs": logs}]}
        phases = [{"phaseType": "PRE_BUILD", "phaseStatus": "SUCCEEDED"}]
        if self.status == "FAILED":
            phases.append({"phaseType": "BUILD", "phaseStatus": "FAILED", "contexts": [
                {"statusCode": "COMMAND_EXECUTION_ERROR",
                 "message": "Error while executing command: docker build. Reason: exit status 1"}]})
        return {"builds": [{"id": "pdt:1", "buildComplete": True, "buildStatus": self.status,
                            "phases": phases, "logs": logs}]}


class FakeLogs:
    """One stream whose lines arrive over two reads."""

    def __init__(self, groups=()):
        self.batches = [["Step 1/5 : FROM base"], ["ERROR: uv sync failed", "last line"]]
        self.groups = list(groups)

    def get_log_events(self, logGroupName, logStreamName, startFromHead, nextToken=None):
        index = int(nextToken or 0)
        if index >= len(self.batches):
            return {"events": [], "nextForwardToken": str(index)}
        return {"events": [{"message": line + "\n"} for line in self.batches[index]],
                "nextForwardToken": str(index + 1)}

    def describe_log_groups(self, logGroupNamePrefix):
        return {"logGroups": [{"logGroupName": name, "arn": f"arn:{name}:*"}
                              for name in self.groups if name.startswith(logGroupNamePrefix)]}

    def list_tags_for_resource(self, resourceArn):
        return {"tags": {"managed-by": "pdt"}}

    def delete_log_group(self, logGroupName):
        self.groups.remove(logGroupName)


class FakeEcr:
    def __init__(self, digest="sha256:abc"):
        self.digest = digest

    def get_authorization_token(self):
        return {"authorizationData": [{"authorizationToken": "QVdTOnNlY3JldA==",
                                       "proxyEndpoint": f"https://{REGISTRY}"}]}

    def describe_images(self, repositoryName, imageIds):
        if self.digest is None:
            raise client_error("ImageNotFoundException")
        return {"imageDetails": [{"imageDigest": self.digest}]}


class FakeIam:
    def __init__(self, roles=()):
        self.roles = set(roles)
        self.deleted = []

    def get_role(self, RoleName):
        if RoleName not in self.roles:
            raise client_error("NoSuchEntity")
        return {"Role": {"RoleName": RoleName, "Tags": MANAGED}}

    def list_role_policies(self, RoleName):
        return {"PolicyNames": []}

    def delete_role(self, RoleName):
        self.deleted.append(RoleName)
        self.roles.remove(RoleName)


@pytest.fixture
def deploy(project, monkeypatch):
    """Run a deploy of `my-app` with the Batch side stubbed; return the fakes it used."""
    app_dir = add_app(project, "my-app")
    (app_dir / "helper.py").write_text("X = 1\n")
    (app_dir / ".env").write_text("TOKEN=secret\n")
    (app_dir / "id_rsa").write_text("PRIVATE KEY\n")
    commands = []
    roles = {}
    log_groups = []
    definitions = []
    for name, value in {
        "ensure_session": lambda app: None,
        "aws_settings": lambda app, session: (ACCOUNT, REGION),
        "preflight": lambda *args: (ACCOUNT, lambda: None),
        "gather_secrets": lambda app: {},
        "cost_estimate_for": lambda *args: None,
        "confirm": lambda actions, assume_yes, estimate: True,
        "default_network": lambda ec2: ("vpc-1", ["subnet-a"]),
        "find_security_group": lambda ec2, vpc_id: None,
        "describe": lambda batch, resource: None,
        "resource_exists": lambda client, operation, **kwargs: False,
        "find_log_group": lambda logs, name: None,
        "ensure_repository": lambda ecr: f"{REGISTRY}/{REPOSITORY}",
        "ensure_security_group": lambda ec2, vpc_id, group: "sg-pdt",
        "ensure_compute_environment": lambda *args: "arn:ce",
        "ensure_job_queue": lambda *args: "arn:queue",
        "ensure_log_group": lambda logs, name: log_groups.append(name),
        "ensure_secret": lambda *args: "arn:secret",
        "ensure_roles": lambda *args: ("arn:execution", "arn:job", "arn:scheduler"),
        "ensure_role": lambda iam, name, service, policy, statements: roles.setdefault(
            name, (service, statements)) and f"arn:aws:iam::{ACCOUNT}:role/{name}",
        "ensure_job_definition": lambda *args: definitions.append(args) or "arn:definition",
        "ensure_schedule": lambda *args, **kwargs: None,
        "BUILD_POLL_SECONDS": 0,
        "BUILD_LOG_SETTLE_SECONDS": 0,
    }.items():
        monkeypatch.setattr(batch_deploy, name, value)
    monkeypatch.setattr(batch_deploy.regions, "local_currency", lambda: "USD")
    monkeypatch.setattr(batch_deploy.subprocess, "run", lambda command, **kwargs: commands.append(
        command) or SimpleNamespace(returncode=0, stdout="", stderr=""))

    def run(cloud: bool, status="SUCCEEDED", revisions=(), digest="sha256:abc"):
        monkeypatch.setattr(batch_deploy, "build_in_cloud",
                            lambda app, builder, image_platform: cloud)
        monkeypatch.setattr(batch_deploy, "active_job_definitions",
                            lambda batch, name=None: list(revisions))
        definitions.clear()
        commands.clear()
        fakes = {"s3": FakeS3(), "codebuild": FakeCodeBuild(status), "logs": FakeLogs(),
                 "ecr": FakeEcr(digest), "iam": FakeIam(), "ec2": None, "batch": None,
                 "scheduler": None, "secretsmanager": None, "sts": None,
                 "definitions": definitions}
        monkeypatch.setattr(batch_deploy, "batch_clients", lambda session: fakes)
        app = {"name": "my-app", "dir": app_dir, "storage": False, "schedule": "0 6 * * *",
               "timezone": "UTC", "pause": False, "platform": {"provider": "aws"}}
        try:
            code = batch_deploy.deploy(app, assume_yes=True)
        except SystemExit:
            code = "failed"
        return code, fakes, commands, roles, log_groups
    return run


def test_a_cloud_build_never_runs_docker_on_this_computer(deploy):
    code, fakes, commands, _roles, _logs = deploy(cloud=True)
    assert code == 0
    assert commands == []
    assert len(fakes["codebuild"].started) == 1


def test_the_upload_holds_only_the_staged_files(deploy):
    _code, fakes, _commands, _roles, _logs = deploy(cloud=True)
    [upload] = fakes["s3"].uploaded.values()
    names = set(zipfile.ZipFile(BytesIO(upload)).namelist())
    assert {"Dockerfile", "pdt.yml", "my-app/run.py", "my-app/helper.py"} <= names
    assert not any(name.endswith((".env", "id_rsa")) for name in names)


def test_the_build_gets_the_upload_the_image_and_an_arm_project(deploy):
    _code, fakes, _commands, _roles, _logs = deploy(cloud=True)
    [started] = fakes["codebuild"].started
    [key] = fakes["s3"].uploaded
    assert started["projectName"] == BUILD_PROJECT
    assert started["sourceTypeOverride"] == "S3"
    assert started["sourceLocationOverride"] == f"{BUCKET}/{key}"
    assert started["environmentVariablesOverride"] == [
        {"name": "IMAGE", "value": IMAGE, "type": "PLAINTEXT"}]
    project = fakes["codebuild"].project
    assert project["environment"]["type"] == "ARM_CONTAINER"
    assert project["environment"]["privilegedMode"] is True
    assert project["logsConfig"]["cloudWatchLogs"] == {
        "status": "ENABLED", "groupName": BUILD_LOG_GROUP}
    assert {"key": "managed-by", "value": "pdt"} in project["tags"]
    commands = yaml.safe_load(started["buildspecOverride"])["phases"]["build"]["commands"]
    assert commands[0] == 'docker build --platform linux/arm64 -t "$IMAGE" .'


def test_the_upload_is_deleted_after_the_build(deploy):
    _code, fakes, _commands, _roles, _logs = deploy(cloud=True)
    [key] = fakes["s3"].uploaded
    assert ("delete_object", key) in fakes["s3"].calls
    assert fakes["s3"].buckets[BUCKET] == {}


def test_a_cloud_deploy_creates_the_build_resources_tagged_and_scoped(deploy):
    _code, fakes, _commands, roles, log_groups = deploy(cloud=True)
    assert BUILD_LOG_GROUP in log_groups
    assert fakes["s3"].calls[:2] == ["create_bucket", "put_public_access_block"]
    assert fakes["s3"].tags[BUCKET] == MANAGED
    assert fakes["s3"].lifecycle[0]["Expiration"] == {"Days": 1}
    service, statements = roles[BUILD_ROLE]
    assert service == "codebuild.amazonaws.com"
    resources = [statement["Resource"] for statement in statements]
    assert resources == ["*", f"arn:aws:ecr:{REGION}:{ACCOUNT}:repository/{REPOSITORY}",
                         f"arn:aws:logs:{REGION}:{ACCOUNT}:log-group:{BUILD_LOG_GROUP}:*",
                         f"arn:aws:s3:::{BUCKET}/*", f"arn:aws:s3:::{BUCKET}"]
    assert fakes["codebuild"].calls == ["create_project"]


def test_a_failed_build_stops_once_with_its_phase_reason_and_log(deploy, capsys):
    code, fakes, commands, _roles, _logs = deploy(cloud=True, status="FAILED")
    out = " ".join(plain([capsys.readouterr().out]))
    assert code == "failed"
    assert len(fakes["codebuild"].started) == 1
    assert commands == []
    assert "ERROR: uv sync failed" in out and "last line" in out
    assert "failed in phase BUILD" in out
    assert "Reason: exit status 1" in out
    assert out.index("last line") < out.index("failed in phase BUILD")
    assert fakes["s3"].buckets[BUCKET] == {}


def test_a_local_build_never_touches_codebuild_or_the_build_bucket(deploy):
    code, fakes, commands, roles, log_groups = deploy(cloud=False)
    assert code == 0
    assert [command[:2] for command in commands] == [
        ["docker", "login"], ["docker", "build"], ["docker", "push"]]
    assert "--platform" in commands[1] and "linux/arm64" in commands[1]
    assert fakes["codebuild"].started == [] and fakes["codebuild"].calls == []
    assert fakes["s3"].buckets == {} and fakes["s3"].calls == []
    assert BUILD_ROLE not in roles and BUILD_LOG_GROUP not in log_groups


def plan_lines(deploy_fixture, monkeypatch, cloud):
    shown = []
    monkeypatch.setattr(batch_deploy, "confirm",
                        lambda actions, assume_yes, estimate: shown.extend(plain(actions)))
    deploy_fixture(cloud=cloud)
    return shown


def test_the_plan_names_where_the_image_builds_and_each_build_resource(deploy, monkeypatch):
    shown = plan_lines(deploy, monkeypatch, cloud=True)
    assert any("with CodeBuild" in line for line in shown)
    assert any(line.startswith(f"create shared CodeBuild project {BUILD_PROJECT}")
               for line in shown)
    assert any(line.startswith(f"create IAM role {BUILD_ROLE}") for line in shown)
    assert any(line.startswith(f"create private S3 bucket {BUCKET}") for line in shown)
    assert any(line.startswith(f"create log group {BUILD_LOG_GROUP}") for line in shown)
    local = plan_lines(deploy, monkeypatch, cloud=False)
    assert any("with Docker on this computer" in line for line in local)
    assert not any("CodeBuild" in line or BUCKET in line for line in local)


def recorded(fingerprint, digest="sha256:abc"):
    return [{"revision": 1, "tags": {"managed-by": "pdt", "image-digest": digest,
                                     BUILD_TAG: fingerprint}}]


@pytest.mark.parametrize("cloud", [True, False])
def test_an_unchanged_app_skips_the_build_and_the_build_resources(deploy, capsys, cloud):
    _code, fakes, _commands, _roles, _logs = deploy(cloud=False)
    [(_batch, _desired, digest, fingerprint)] = fakes["definitions"]
    assert digest == "sha256:abc" and len(fingerprint) == 32
    capsys.readouterr()
    code, fakes, commands, roles, log_groups = deploy(cloud=cloud, revisions=recorded(fingerprint))
    out = " ".join(plain([capsys.readouterr().out]))
    assert code == 0
    assert commands == []
    assert fakes["codebuild"].started == [] and fakes["codebuild"].calls == []
    assert fakes["s3"].calls == [] and BUILD_ROLE not in roles
    assert BUILD_LOG_GROUP not in log_groups
    assert f"Image {IMAGE} is unchanged; skipped the build and push." in out
    assert fakes["definitions"][0][2:] == ("sha256:abc", fingerprint)


def test_a_changed_app_builds_and_records_the_new_fingerprint(deploy):
    _code, fakes, commands, _roles, _logs = deploy(cloud=False, revisions=recorded("0" * 32))
    assert [command[:2] for command in commands][1:] == [["docker", "build"], ["docker", "push"]]
    assert fakes["definitions"][0][3] != "0" * 32


def test_a_job_whose_image_left_the_registry_builds_again(deploy, capsys):
    _code, fakes, _commands, _roles, _logs = deploy(cloud=False)
    fingerprint = fakes["definitions"][0][3]
    capsys.readouterr()
    _code, fakes, commands, _roles, _logs = deploy(
        cloud=False, revisions=recorded(fingerprint, "sha256:old"))
    out = " ".join(plain([capsys.readouterr().out]))
    assert "the registry no longer holds the image the job runs" in out
    assert [command[:2] for command in commands][1:] == [["docker", "build"], ["docker", "push"]]


def test_the_plan_says_the_build_runs_only_when_the_app_changed(deploy, monkeypatch):
    shown = []
    monkeypatch.setattr(batch_deploy, "confirm",
                        lambda actions, assume_yes, estimate: shown.extend(plain(actions)))
    deploy(cloud=False, revisions=recorded("0" * 32))
    assert any(line.endswith("with Docker on this computer, if it changed") for line in shown)
    shown.clear()
    monkeypatch.setenv(REBUILD_ENV, "1")
    deploy(cloud=False, revisions=recorded("0" * 32))
    assert any(line.endswith("with Docker on this computer") for line in shown)


@pytest.fixture
def destroy(monkeypatch):
    monkeypatch.setattr(batch_deploy, "ensure_session", lambda app: None)
    monkeypatch.setattr(batch_deploy, "aws_settings", lambda app, session: (ACCOUNT, REGION))
    monkeypatch.setattr(batch_deploy, "preflight", lambda *args: (ACCOUNT, lambda: None))
    monkeypatch.setattr(batch_deploy, "confirm", lambda actions, assume_yes: True)
    monkeypatch.setattr(batch_deploy, "legacy_fargate_cleanup", lambda *args: [])
    monkeypatch.setattr(batch_deploy, "other_schedules", lambda scheduler, name: ["pdt-other"])
    monkeypatch.setattr(batch_deploy, "active_job_definitions", lambda batch, name=None: [])
    monkeypatch.setattr(batch_deploy, "shared_present", lambda batch: [])
    monkeypatch.setattr(batch_deploy, "default_vpc", lambda ec2: None)

    class Ignore:
        def __getattr__(self, operation):
            def call(**kwargs):
                if operation in ("get_schedule", "describe_secret"):
                    raise client_error("ResourceNotFoundException")
                return {}
            return call

    class Ecr(Ignore):
        def __init__(self, others):
            self.others = others

        def list_images(self, repositoryName, filter):
            return {"imageIds": [{"imageTag": "my-app-latest"},
                                 *({"imageTag": "other-latest"} for _ in range(self.others))]}

    def run(fakes, others=0):
        clients = {"scheduler": Ignore(), "secretsmanager": Ignore(), "batch": Ignore(),
                   "ecs": Ignore(), "ec2": Ignore(), "ecr": Ecr(others), "sts": None, **fakes}
        monkeypatch.setattr(batch_deploy, "batch_clients", lambda session: clients)
        return batch_deploy.destroy({"name": "my-app", "storage": False}, assume_yes=True)
    return run


def built_once():
    return {"codebuild": FakeCodeBuild(project={"name": BUILD_PROJECT, "tags": [
                {"key": "managed-by", "value": "pdt"}]}),
            "iam": FakeIam([BUILD_ROLE]), "s3": FakeS3(present=True),
            "logs": FakeLogs([BUILD_LOG_GROUP])}


def test_destroy_of_the_last_app_removes_every_build_resource(destroy):
    fakes = built_once()
    fakes["s3"].buckets[BUCKET]["my-app/left.zip"] = b"zip"
    assert destroy(fakes) == 0
    assert fakes["codebuild"].project is None
    assert BUILD_ROLE in fakes["iam"].deleted
    assert fakes["s3"].buckets == {}
    assert BUILD_LOG_GROUP not in fakes["logs"].groups


def test_destroy_keeps_the_build_resources_while_another_app_has_an_image(destroy):
    fakes = built_once()
    assert destroy(fakes, others=1) == 0
    assert fakes["codebuild"].project is not None
    assert fakes["iam"].deleted == []
    assert BUCKET in fakes["s3"].buckets
    assert BUILD_LOG_GROUP in fakes["logs"].groups


def test_destroy_without_any_cloud_build_does_not_fail(destroy):
    fakes = {"codebuild": FakeCodeBuild(), "iam": FakeIam(), "s3": FakeS3(),
             "logs": FakeLogs()}
    assert destroy(fakes) == 0
    assert fakes["codebuild"].calls == []
    assert fakes["iam"].deleted == []


def test_destroy_leaves_a_project_pdt_did_not_create(destroy):
    fakes = built_once()
    fakes["codebuild"].project["tags"] = []
    assert destroy(fakes) == 0
    assert fakes["codebuild"].project is not None


def test_the_deployer_policy_grants_the_build_actions_on_pdt_resources_only():
    statements = deployer_policy(DEPLOYER_ACTIONS, ACCOUNT, REGION)["Statement"]
    granted = {action: statement["Resource"] for statement in statements
               for action in statement["Action"]}
    assert granted["codebuild:StartBuild"] == [
        f"arn:aws:codebuild:{REGION}:{ACCOUNT}:project/{BUILD_PROJECT}"]
    for action in ("codebuild:BatchGetBuilds", "codebuild:BatchGetProjects",
                   "codebuild:CreateProject", "codebuild:UpdateProject",
                   "codebuild:DeleteProject", "s3:PutLifecycleConfiguration",
                   "s3:DeleteBucket"):
        assert action in granted
    assert granted["s3:PutObject"] == ["arn:aws:s3:::pdt-build-*/*"]
    assert granted["s3:PutLifecycleConfiguration"] == ["arn:aws:s3:::pdt-build-*"]
    assert "logs:GetLogEvents" in granted
    [pass_role] = [statement for statement in statements if statement["Action"] == ["iam:PassRole"]]
    assert "codebuild.amazonaws.com" in pass_role["Condition"]["StringEquals"][
        "iam:PassedToService"]


def test_the_buildspec_logs_in_to_the_image_registry_and_pushes_the_image():
    phases = yaml.safe_load(BUILDSPEC)["phases"]
    assert "docker login" in phases["pre_build"]["commands"][0]
    assert phases["build"]["commands"][-1] == 'docker push "$IMAGE"'
