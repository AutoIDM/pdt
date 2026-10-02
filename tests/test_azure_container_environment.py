from pdt import deploy_azure, deploy_azure_container_apps

SUBSCRIPTION = "11111111-1111-1111-1111-111111111111"
SHARED = deploy_azure.Environment("pdt-shared", "pdt-eastus2", True)


def test_job_creation_uses_the_environment_resource_id(monkeypatch):
    """A job must name the environment by resource id, because the shared
    environment lives in another resource group than the app's."""
    calls = []
    settings = {
        "subscription": SUBSCRIPTION, "resource_group": "pdt",
        "registry": "pdtregistry", "environment": SHARED,
    }
    monkeypatch.setattr(
        deploy_azure_container_apps, "run_quiet",
        lambda *args, **kwargs: calls.append(args))

    deploy_azure_container_apps.reconcile_job(
        settings, "pdt-report", "pdtregistry.azurecr.io/report:latest",
        "0 0 * * *", "/identity/pdt-runner", None, None, False, "report")

    args = calls[0]
    assert args[args.index("--environment") + 1] == SHARED.resource_id(SUBSCRIPTION)
