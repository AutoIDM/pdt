import boto3
import botocore.endpoint
from botocore.awsrequest import AWSResponse

from pdt import deploy_aws
from pdt.deploy_aws_batch import DEPLOYER_ACTIONS, batch_clients

ACCOUNT = "123456789012"
THROTTLED = (b"<ErrorResponse><Error><Type>Sender</Type><Code>Throttling</Code>"
             b"<Message>Rate exceeded</Message></Error><RequestId>1</RequestId></ErrorResponse>")
ALLOWED = (b'<SimulatePrincipalPolicyResponse xmlns="https://iam.amazonaws.com/doc/2010-05-08/">'
           b"<SimulatePrincipalPolicyResult><IsTruncated>false</IsTruncated><EvaluationResults/>"
           b"</SimulatePrincipalPolicyResult><ResponseMetadata><RequestId>1</RequestId>"
           b"</ResponseMetadata></SimulatePrincipalPolicyResponse>")


class Body:
    def __init__(self, content: bytes):
        self.content = content

    def stream(self, **_kwargs):
        yield self.content


class Sts:
    def get_caller_identity(self):
        return {"Account": ACCOUNT, "Arn": f"arn:aws:iam::{ACCOUNT}:user/operator"}


def session(monkeypatch, tmp_path):
    for name in ("AWS_RETRY_MODE", "AWS_MAX_ATTEMPTS", "AWS_PROFILE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AWS_CONFIG_FILE", str(tmp_path / "config"))
    return boto3.Session(aws_access_key_id="key", aws_secret_access_key="secret",
                         region_name="us-east-1")


def test_preflight_waits_out_a_throttled_permission_check(monkeypatch, tmp_path):
    monkeypatch.setattr(botocore.endpoint.time, "sleep", lambda _seconds: None)
    iam = deploy_aws.clients_for(session(monkeypatch, tmp_path))["iam"]
    sent = []

    def respond(request, **_kwargs):
        sent.append(request)
        if len(sent) <= 8:
            return AWSResponse(request.url, 400, {}, Body(THROTTLED))
        return AWSResponse(request.url, 200, {}, Body(ALLOWED))

    iam.meta.events.register("before-send.iam.SimulatePrincipalPolicy", respond)

    account, _identity = deploy_aws.preflight(Sts(), iam, ACCOUNT, "us-east-1", DEPLOYER_ACTIONS)

    assert account == ACCOUNT
    assert len(sent) == 8 + len(deploy_aws.simulations(
        deploy_aws.deployer_policy(DEPLOYER_ACTIONS, ACCOUNT, "us-east-1")))


def test_every_deploy_and_destroy_client_uses_standard_retries(monkeypatch, tmp_path):
    clients = batch_clients(session(monkeypatch, tmp_path))

    assert {name: client.meta.config.retries["mode"] for name, client in clients.items()} == {
        name: "standard" for name in
        ("sts", "logs", "secretsmanager", "iam", "scheduler", "s3", "batch", "ec2", "ecr", "ecs")}
