"""Every cloud deploy ends with a Run logs line naming where run output lives.

The links are built from settings alone, never from the pre-deploy resource
state, so they exist on a first deploy, before the resources do.
"""

import sys
import types

# deploy_aws imports boto3 at module level; the link formats need no AWS SDK.
if "botocore.exceptions" not in sys.modules:
    sys.modules.setdefault("boto3", types.ModuleType("boto3"))
    exceptions = types.ModuleType("botocore.exceptions")
    exceptions.ClientError = type("ClientError", (Exception,), {})
    botocore = types.ModuleType("botocore")
    botocore.exceptions = exceptions
    sys.modules.setdefault("botocore", botocore)
    sys.modules["botocore.exceptions"] = exceptions

from pdt.deploy_aws import log_group_url
from pdt.deploy_azure_container_apps import job_history_url
from pdt.deploy_google_cloud import job_logs_url

AZURE = {"subscription": "79ee0bd6-0000-0000-0000-000000000000",
         "resource_group": "pdt"}


def test_azure_container_apps_link_lands_on_the_job_page():
    url = job_history_url(AZURE, "pdt-hello-world")
    assert url == (
        "https://portal.azure.com/#resource"
        "/subscriptions/79ee0bd6-0000-0000-0000-000000000000"
        "/resourceGroups/pdt/providers/Microsoft.App/jobs/pdt-hello-world"
    )


def test_cloudwatch_link_double_encodes_the_log_group():
    url = log_group_url("us-east-1", "/pdt/hello-world")
    assert url == (
        "https://us-east-1.console.aws.amazon.com/cloudwatch/home"
        "?region=us-east-1#logsV2:log-groups/log-group"
        "/$252Fpdt$252Fhello-world"
    )


def test_cloud_run_link_lands_on_the_job_logs_tab():
    url = job_logs_url("my-project", "us-central1", "pdt-hello-world")
    assert url == (
        "https://console.cloud.google.com/run/jobs/details/us-central1"
        "/pdt-hello-world/logs?project=my-project"
    )
