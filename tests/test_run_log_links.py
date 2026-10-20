"""The console link to each cloud provider's run logs.

The links are built from settings alone, never from the resource state, so
they exist before the resources do.
"""

from pdt import deploy_azure
from pdt.deploy_aws import log_group_url
from pdt.deploy_azure import parse_environment
from pdt.deploy_azure_container_apps import job_history_url, logs_query_url
from pdt.deploy_google_cloud import job_logs_url

AZURE = {"subscription": "79ee0bd6-0000-0000-0000-000000000000",
         "tenant": "2f4a9838-0000-0000-0000-000000000000",
         "resource_group": "pdt", "workspace": "pdt-logs",
         "environment": parse_environment("", "eastus")}


def test_azure_container_apps_link_lands_on_the_execution_history_tab():
    url = job_history_url(AZURE, "pdt-hello-world")
    assert url == (
        "https://portal.azure.com/#@2f4a9838-0000-0000-0000-000000000000/resource"
        "/subscriptions/79ee0bd6-0000-0000-0000-000000000000"
        "/resourceGroups/pdt/providers/Microsoft.App/jobs/pdt-hello-world/executionHistory"
    )


def test_every_azure_portal_link_names_the_tenant(monkeypatch):
    pages = []
    monkeypatch.setattr(deploy_azure, "store_settings",
                        lambda settings: {"account_id": "/subscriptions/x/storageAccounts/pdtx",
                                          "container": "pdt"})
    monkeypatch.setattr(deploy_azure, "cli_credential_store", lambda settings, name: None)
    monkeypatch.setattr(deploy_azure.storage_cli, "run",
                        lambda store, app, rest, assume_yes, page: pages.append(page) or 0)
    deploy_azure.storage({"name": "hello-world"}, AZURE, ["--open"], False)
    links = [job_history_url(AZURE, "pdt-hello-world"), logs_query_url(AZURE, "pdt-hello-world"),
             logs_query_url(AZURE, "pdt-hello-world", "pdt-hello-world-abc12"), *pages]
    assert len(links) == 4
    for link in links:
        assert link.startswith("https://portal.azure.com/#@2f4a9838-0000-0000-0000-000000000000/")


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
