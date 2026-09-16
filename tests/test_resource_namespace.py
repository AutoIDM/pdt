import hashlib

import pytest

from pdt import deploy_common
from pdt import deploy_aws_fargate
from pdt import deploy_azure
from pdt import deploy_google_cloud
import inventory


def test_default_resource_names_are_unchanged(monkeypatch):
    monkeypatch.delenv("PDT_RESOURCE_NAMESPACE", raising=False)
    assert deploy_common.resource_prefix() == "pdt"
    assert deploy_common.resource_name("report") == "pdt-report"


def test_namespaces_produce_distinct_resource_names(monkeypatch):
    monkeypatch.setenv("PDT_RESOURCE_NAMESPACE", "101")
    first = deploy_aws_fargate.resource_names("report")
    monkeypatch.setenv("PDT_RESOURCE_NAMESPACE", "202")
    second = deploy_aws_fargate.resource_names("report")
    assert first["family"] == "pdt-101-report"
    assert second["family"] == "pdt-202-report"
    assert first["legacy_log_group"] != second["legacy_log_group"]


def test_namespaces_produce_distinct_physical_stores(monkeypatch):
    monkeypatch.setenv("PDT_RESOURCE_NAMESPACE", "101")
    first = deploy_common.store_name("account")
    first_azure = deploy_azure.store_settings({"subscription": "sub"})
    monkeypatch.setenv("PDT_RESOURCE_NAMESPACE", "202")
    second = deploy_common.store_name("account")
    second_azure = deploy_azure.store_settings({"subscription": "sub"})
    assert first != second
    assert first_azure["group"] != second_azure["group"]
    assert first_azure["account"] != second_azure["account"]
    assert first_azure["container"] != second_azure["container"]


def test_namespace_rejects_values_that_break_provider_names(monkeypatch):
    monkeypatch.setenv("PDT_RESOURCE_NAMESPACE", "CI_JOB_ID")
    with pytest.raises(ValueError, match="PDT_RESOURCE_NAMESPACE"):
        deploy_common.resource_prefix()


def test_cleanup_only_considers_jobs_in_the_active_namespace(monkeypatch):
    monkeypatch.setenv("PDT_RESOURCE_NAMESPACE", "101")
    assert deploy_google_cloud.same_namespace_job({"name": "pdt-101-report"})
    assert not deploy_google_cloud.same_namespace_job({"name": "pdt-202-report"})


def test_default_cleanup_preserves_the_previous_safety_scope(monkeypatch):
    monkeypatch.delenv("PDT_RESOURCE_NAMESPACE", raising=False)
    assert deploy_google_cloud.same_namespace_job({"name": "another-job"})


def test_inventory_ownership_uses_the_active_namespace(monkeypatch):
    monkeypatch.setenv("PDT_RESOURCE_NAMESPACE", "101")
    assert inventory.has_prefix("pdt-101-report")
    assert not inventory.has_prefix("pdt-202-report")


def test_azure_run_lookup_uses_the_provider_shortened_name(monkeypatch):
    monkeypatch.setenv("PDT_RESOURCE_NAMESPACE", "123456789012")
    name = inventory.azure_job_name("azure-container-apps-a")
    assert len(name) == 32
    assert name.startswith("pdt-123456789012-")


def test_azure_deleted_vault_inventory_uses_the_resource_group_hash(monkeypatch):
    settings = {"subscription": "sub", "resource_group": "pdt-101"}
    suffix = hashlib.sha256(b"sub/pdt-101").hexdigest()[:10]
    expected = f"pdt-{suffix}"
    monkeypatch.setattr(inventory, "az", lambda *_args: [
        {"name": expected, "id": "/expected", "properties": {"tags": {}}},
        {"name": "pdt-other", "id": "/other", "properties": {"tags": {}}},
    ])
    assert [resource.name for resource in inventory.azure_deleted_vaults(settings)] == [expected]
