import copy
import json

from pdt.deploy_aws_batch import (
    desired_job_definition, ensure_job_definition, normalized_job_definition, resource_names,
    same_job_definition, submit_job_target,
)

NAMES = resource_names("my-app")
DIGEST = "sha256:abc"


def desired(environment=None):
    return desired_job_definition(
        NAMES, "1.dkr.ecr.us-east-1.amazonaws.com/pdt:my-app-latest", "us-east-1",
        "arn:execution", "arn:job", "arn:secret",
        environment or {"PDT_ENV_SECRET_RESOURCE": "arn:secret"})


def described(definition, revision=1, digest=DIGEST, tags=None):
    """The shape describe_job_definitions returns for a registered definition."""
    current = copy.deepcopy(definition)
    current["containerProperties"]["command"] = []
    current["containerProperties"]["volumes"] = []
    current["containerProperties"]["logConfiguration"]["secretOptions"] = []
    current["containerProperties"]["environment"].reverse()
    current["containerProperties"]["resourceRequirements"].reverse()
    current.update({
        "jobDefinitionArn": f"arn:aws:batch:us-east-1:1:job-definition/pdt-my-app:{revision}",
        "revision": revision, "status": "ACTIVE",
        "tags": {"managed-by": "pdt", "image-digest": digest} if tags is None else tags,
    })
    return current


class FakeBatch:
    def __init__(self, revisions=()):
        self.revisions = list(revisions)
        self.registered = []
        self.deregistered = []

    def describe_job_definitions(self, **kwargs):
        return {"jobDefinitions": self.revisions}

    def register_job_definition(self, **kwargs):
        self.registered.append(kwargs)
        return {"jobDefinitionArn": "arn:aws:batch:us-east-1:1:job-definition/pdt-my-app:9",
                "revision": 9}

    def deregister_job_definition(self, jobDefinition):
        self.deregistered.append(jobDefinition)


def test_the_schedule_submits_the_job_by_name_to_the_shared_queue():
    target = submit_job_target(NAMES)
    assert target["Arn"] == "arn:aws:scheduler:::aws-sdk:batch:submitJob"
    assert json.loads(target["Input"]) == {
        "JobName": "pdt-my-app", "JobQueue": "pdt", "JobDefinition": "pdt-my-app"}


def test_the_job_definition_runs_the_image_on_fargate_arm64_with_the_env_secret():
    container = desired()["containerProperties"]
    assert desired()["platformCapabilities"] == ["FARGATE"]
    assert container["runtimePlatform"]["cpuArchitecture"] == "ARM64"
    assert container["secrets"] == [{"name": "PDT_ENV_JSON", "valueFrom": "arn:secret"}]
    assert container["logConfiguration"]["options"]["awslogs-group"] == "/pdt/my-app"
    assert {item["type"]: item["value"] for item in container["resourceRequirements"]} == {
        "VCPU": "0.25", "MEMORY": "512"}


def test_the_described_shape_normalizes_to_the_desired_shape():
    assert normalized_job_definition(described(desired())) == normalized_job_definition(desired())


def test_a_same_image_and_same_properties_needs_no_new_revision():
    assert same_job_definition(described(desired()), desired(), DIGEST)


def test_a_new_image_digest_needs_a_new_revision():
    assert not same_job_definition(described(desired(), digest="sha256:old"), desired(), DIGEST)


def test_a_changed_environment_needs_a_new_revision():
    current = described(desired({"PDT_ENV_SECRET_RESOURCE": "arn:secret",
                                 "PDT_STORAGE_URL": "s3://b/my-app/"}))
    assert not same_job_definition(current, desired(), DIGEST)


def test_ensure_keeps_the_current_revision_when_nothing_changed():
    batch = FakeBatch([described(desired())])
    arn = ensure_job_definition(batch, desired(), DIGEST)
    assert arn.endswith(":1")
    assert batch.registered == []
    assert batch.deregistered == []


def test_ensure_registers_a_revision_and_deregisters_the_tagged_older_ones():
    batch = FakeBatch([described(desired(), revision=2, digest="sha256:old"),
                       described(desired(), revision=1, digest="sha256:older",
                                 tags={"other": "owner"})])
    arn = ensure_job_definition(batch, desired(), DIGEST)
    assert arn.endswith(":9")
    [registered] = batch.registered
    assert registered["tags"] == {"managed-by": "pdt", "image-digest": DIGEST}
    assert registered["jobDefinitionName"] == "pdt-my-app"
    assert batch.deregistered == ["arn:aws:batch:us-east-1:1:job-definition/pdt-my-app:2"]


def test_ensure_registers_the_first_revision_when_none_exists():
    batch = FakeBatch()
    ensure_job_definition(batch, desired(), DIGEST)
    assert len(batch.registered) == 1
