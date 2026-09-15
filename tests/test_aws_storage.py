import sys
import types

# deploy_aws imports boto3 at module level; the store shapes need no AWS SDK.
if "botocore.exceptions" not in sys.modules:
    sys.modules.setdefault("boto3", types.ModuleType("boto3"))
    exceptions = types.ModuleType("botocore.exceptions")
    exceptions.ClientError = type("ClientError", (Exception,), {})
    botocore = types.ModuleType("botocore")
    botocore.exceptions = exceptions
    sys.modules.setdefault("botocore", botocore)
    sys.modules["botocore.exceptions"] = exceptions

from pdt.deploy_aws import COMMON_ACTIONS, store_statements, store_url
from pdt.deploy_common import store_name, store_suffix

ACCOUNT = "123456789012"


def test_the_bucket_name_derives_from_the_account_alone():
    assert store_name(ACCOUNT) == f"pdt-data-{store_suffix(ACCOUNT)}"
    assert len(store_suffix(ACCOUNT)) == 10
    assert store_name(ACCOUNT) != store_name("210987654321")


def test_the_job_sees_its_own_folder_with_a_trailing_slash():
    assert store_url("pdt-data-abc", "hello-world") == "s3://pdt-data-abc/hello-world/"


def test_the_grant_stops_at_the_app_folder():
    objects, listing = store_statements("pdt-data-abc", "hello-world")
    assert objects["Resource"] == "arn:aws:s3:::pdt-data-abc/hello-world/*"
    assert set(objects["Action"]) == {"s3:GetObject", "s3:PutObject", "s3:DeleteObject"}
    assert listing["Resource"] == "arn:aws:s3:::pdt-data-abc"
    assert listing["Action"] == ["s3:ListBucket"]
    assert listing["Condition"] == {"StringLike": {"s3:prefix": ["hello-world/*"]}}


def test_the_deployer_policy_covers_the_bucket_calls():
    for action in ("s3:CreateBucket", "s3:PutBucketTagging", "s3:GetBucketTagging",
                   "s3:PutBucketPublicAccessBlock", "s3:ListBucket"):
        assert action in COMMON_ACTIONS
