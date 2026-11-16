import json
import sys
import types

import pytest

# deploy_aws imports boto3 at module level; the store shapes need no AWS SDK.
if "botocore.exceptions" not in sys.modules:
    sys.modules.setdefault("boto3", types.ModuleType("boto3"))
    exceptions = types.ModuleType("botocore.exceptions")
    exceptions.ClientError = type("ClientError", (Exception,), {})
    botocore = types.ModuleType("botocore")
    botocore.exceptions = exceptions
    sys.modules.setdefault("botocore", botocore)
    sys.modules["botocore.exceptions"] = exceptions

from pdt.deploy_aws import check_policy_size, secret_statements, store_statements, store_url
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


def test_the_grant_adds_read_access_to_each_named_folder():
    objects, listing, reads = store_statements("pdt-data-abc", "report", ["client1", "client2"])
    assert objects["Resource"] == "arn:aws:s3:::pdt-data-abc/report/*"
    assert listing["Condition"] == {
        "StringLike": {"s3:prefix": ["report/*", "client1/*", "client2/*"]}}
    assert reads["Action"] == ["s3:GetObject"]
    assert reads["Resource"] == ["arn:aws:s3:::pdt-data-abc/client1/*",
                                 "arn:aws:s3:::pdt-data-abc/client2/*"]


def test_the_policy_text_names_only_the_plain_folders():
    assert json.dumps(store_statements("pdt-data-abc", "report", ["client1"]),
                      separators=(",", ":")) == (
        '[{"Effect":"Allow","Action":["s3:GetObject","s3:PutObject","s3:DeleteObject"],'
        '"Resource":"arn:aws:s3:::pdt-data-abc/report/*"},'
        '{"Effect":"Allow","Action":["s3:ListBucket"],"Resource":"arn:aws:s3:::pdt-data-abc",'
        '"Condition":{"StringLike":{"s3:prefix":["report/*","client1/*"]}}},'
        '{"Effect":"Allow","Action":["s3:GetObject"],'
        '"Resource":["arn:aws:s3:::pdt-data-abc/client1/*"]}]')


def test_a_policy_longer_than_aws_allows_stops_the_deploy(capsys):
    secret = f"arn:aws:secretsmanager:us-east-1:{ACCOUNT}:secret:pdt-report-env-XXXXXX"
    names = [f"client-{number:02d}-" + "x" * 40 for number in range(70)]
    check_policy_size("report", names[:60], secret_statements(secret)
                      + store_statements(store_name(ACCOUNT), "report", names[:60]))
    with pytest.raises(SystemExit):
        check_policy_size("report", names, secret_statements(secret)
                          + store_statements(store_name(ACCOUNT), "report", names))
    assert " ".join(capsys.readouterr().out.split()).endswith(
        "storage.read in report/pdt.yml names 70 apps. The access policy that names them is "
        "10567 characters long, and AWS allows 10240 characters. Remove names from the list.")
