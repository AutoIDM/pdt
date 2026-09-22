import json

import pytest

from pdt import deploy_azure, deploy_azure_container_apps


SETTINGS = {
    "subscription": "sub-1",
    "resource_group": "pdt",
    "suffix": "abc1234def",
}


def test_failed_job_diagnostics_project_only_safe_fields(monkeypatch, capsys):
    calls = []

    def read(*args):
        calls.append(args)
        if args[:3] == ("containerapp", "job", "show"):
            return {
                "id": "/subscriptions/sub-1/resourceGroups/pdt/providers/Microsoft.App/jobs/pdt-report",
                "provisioningState": "Failed",
                "environmentId": "/subscriptions/sub-1/resourceGroups/pdt/providers/Microsoft.App/managedEnvironments/pdt",
                "identityType": "UserAssigned",
                "userAssignedIdentities": {"/subscriptions/sub-1/resourceGroups/pdt/providers/Microsoft.ManagedIdentity/userAssignedIdentities/pdt-runner": {"secret": "omit"}},
                "tags": {"managed-by": "pdt", "pdt-app": "report", "secret": "omit"},
                "secrets": {"pdt-env": "omit"},
            }
        return [{
            "eventTimestamp": "2026-09-14T23:06:27Z",
            "operationName": "Microsoft.App/jobs/write",
            "status": "Failed",
            "subStatus": "InternalServerError",
            "correlationId": "corr-1",
            "resourceId": "/subscriptions/sub-1/resourceGroups/pdt/providers/Microsoft.App/jobs/pdt-report",
            "statusMessage": json.dumps({
                "error": {"code": "InternalServerError", "message": "backend failed",
                          "details": [{"code": "Nested", "message": "retry"},
                                      {"secret": "omit"}]},
            }),
            "properties": {"authorization": "omit", "requestBody": "omit"},
        }, {
            "eventTimestamp": "2026-09-14T23:06:28Z",
            "resourceId": "/subscriptions/sub-1/resourceGroups/pdt/providers/Microsoft.App/jobs/pdt-report-other",
            "correlationId": "wrong-job",
        }]

    monkeypatch.setattr(deploy_azure_container_apps, "az_json", read)
    deploy_azure_container_apps.report_job_failure(SETTINGS, "pdt-report")

    output = capsys.readouterr().out
    assert "provisioning state: Failed" in output
    assert "corr-1" in output
    assert "backend failed" in output
    assert "Nested" in output
    assert "pdt-runner" in output
    assert "wrong-job" not in output
    assert "secret" not in output
    assert "requestBody" not in output
    assert calls[0][-2] == "--query"


def test_diagnostic_query_failure_does_not_raise(monkeypatch, capsys):
    def read(*args):
        raise RuntimeError("secret")

    monkeypatch.setattr(deploy_azure_container_apps, "az_json", read)

    deploy_azure_container_apps.report_job_failure(SETTINGS, "pdt-report")

    output = capsys.readouterr().out
    assert "job state is unavailable" in output
    assert "activity log is unavailable" in output
    assert "secret" not in output


def test_malformed_job_fields_do_not_raise(monkeypatch, capsys):
    monkeypatch.setattr(
        deploy_azure_container_apps, "az_json",
        lambda *args: {"tags": "malformed", "userAssignedIdentities": "malformed"}
        if args[:3] == ("containerapp", "job", "show") else [])

    deploy_azure_container_apps.report_job_failure(SETTINGS, "pdt-report")

    output = capsys.readouterr().out
    assert "owned tags: {}" in output
    assert "user assigned identities: []" in output


def test_deploy_preserves_original_failure_after_reporting(monkeypatch):
    def reconcile(*args):
        raise SystemExit(7)

    monkeypatch.setattr(deploy_azure_container_apps, "reconcile_job", reconcile)
    reported = []
    monkeypatch.setattr(deploy_azure_container_apps, "report_job_failure",
                        lambda settings, job: reported.append((settings, job)))
    settings = SETTINGS | {
        "region": "eastus", "registry": "pdtregistry",
        "environment": deploy_azure.Environment("pdt-shared", "pdt-eastus", True),
        "identity": "pdt-runner", "workspace": "pdt-logs", "vault": "pdt-vault",
    }
    monkeypatch.setattr(deploy_azure_container_apps, "preflight",
                        lambda app, requested: settings)
    monkeypatch.setattr(deploy_azure_container_apps, "azure_settings", lambda app: {})
    monkeypatch.setattr(deploy_azure_container_apps, "gather_secrets", lambda app: {})
    monkeypatch.setattr(deploy_azure_container_apps, "check_shared_names", lambda settings: None)
    def read(*args):
        if args[:2] == ("identity", "create"):
            return {"id": "/identity/pdt-runner", "principalId": "principal-1"}
        return None

    monkeypatch.setattr(deploy_azure_container_apps, "az_json", read)
    monkeypatch.setattr(deploy_azure_container_apps, "workspace_resource", lambda settings: None)
    monkeypatch.setattr(deploy_azure_container_apps, "secret_state", lambda *args: (False, None))
    monkeypatch.setattr(deploy_azure_container_apps, "confirm", lambda *args: True)
    monkeypatch.setattr(deploy_azure_container_apps, "image_action", lambda app, text: text)
    monkeypatch.setattr(deploy_azure_container_apps, "cost_estimate_for", lambda *args: None)
    monkeypatch.setattr(deploy_azure_container_apps, "ensure_group_and_vault", lambda *args: "/vault")
    monkeypatch.setattr(deploy_azure_container_apps, "run_quiet", lambda *args, **kwargs: "")
    monkeypatch.setattr(deploy_azure_container_apps, "ensure_environment", lambda *args: None)
    monkeypatch.setattr(deploy_azure_container_apps, "assign_role", lambda *args: None)
    monkeypatch.setattr(deploy_azure_container_apps, "enable_acr_arm_auth", lambda *args: None)
    monkeypatch.setattr(deploy_azure_container_apps, "build_image", lambda *args: None)
    monkeypatch.setattr(deploy_azure_container_apps, "ensure_secret", lambda *args: None)

    with pytest.raises(SystemExit) as error:
        deploy_azure_container_apps.deploy(
            {"name": "report", "schedule": "0 0 * * *", "storage": False}, True)

    assert error.value.code == 7
    assert reported and reported[0][1] == "pdt-report-abc1234"
