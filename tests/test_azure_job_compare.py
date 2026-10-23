import copy
import json

import pytest

from pdt import deploy_azure
from pdt import deploy_azure_container_apps as azure

SETTINGS = {"resource_group": "pdt", "subscription": "sub-1", "registry": "pdtregistry",
            "environment": deploy_azure.Environment("pdt-shared", "pdt-eastus2", True)}
JOB = "pdt-report-1234567"
IDENTITY = "/subscriptions/sub-1/resourceGroups/pdt/providers/Microsoft.ManagedIdentity/userAssignedIdentities/pdt-runner"
IMAGE = "pdtregistry.azurecr.io/report@sha256:aaa"
SECRET = "https://pdt-vault.vault.azure.net/secrets/pdt-report"
STORAGE = "abfs://pdt-data@pdtdata.dfs.core.windows.net/report/"
FINGERPRINT = "f" * 32

# A job as `az containerapp job show` returns it after a deploy: Azure's own forms of the
# values deploy sent (ids in lower case, "1Gi", 0.5, a space in the identity type).
CURRENT = {
    "identity": {
        "type": "SystemAssigned, UserAssigned",
        "principalId": "job-principal",
        "userAssignedIdentities": {IDENTITY.replace("resourceGroups", "resourcegroups"): {
            "clientId": "client", "principalId": "runner-principal"}},
    },
    "name": JOB,
    "tags": {"managed-by": "pdt", "pdt-app": "report", "pdt-build": FINGERPRINT},
    "properties": {
        "provisioningState": "Succeeded",
        "configuration": {
            "triggerType": "Schedule", "replicaTimeout": 1800, "replicaRetryLimit": 0,
            "manualTriggerConfig": None, "eventTriggerConfig": None,
            "scheduleTriggerConfig": {"cronExpression": "0 0 * * *", "parallelism": 1,
                                      "replicaCompletionCount": 1},
            "registries": [{"server": "pdtregistry.azurecr.io", "username": "",
                            "passwordSecretRef": "",
                            "identity": IDENTITY.replace("resourceGroups", "resourcegroups")}],
            "secrets": [{"name": "pdt-env", "keyVaultUrl": SECRET,
                         "identity": IDENTITY.replace("resourceGroups", "resourcegroups")}],
        },
        "template": {"containers": [{
            "name": JOB, "image": IMAGE,
            "env": [{"name": "PDT_ENV_JSON", "secretRef": "pdt-env"},
                    {"name": "PDT_ENV_SECRET_RESOURCE", "value": SECRET},
                    {"name": "PDT_STORAGE_URL", "value": STORAGE}],
            "resources": {"cpu": 0.5, "memory": "1Gi", "ephemeralStorage": ""},
        }], "initContainers": None, "volumes": None},
    },
}


def body(**changes):
    args = {"registry": "pdtregistry", "job": JOB, "image": IMAGE, "cron": "0 0 * * *",
            "identity_id": IDENTITY, "secret_uri": SECRET, "storage_url": STORAGE,
            "app_name": "report", "fingerprint": FINGERPRINT} | changes
    return azure.job_body(**args)


def changed(path, value):
    """CURRENT with the value at `path` (keys and list indexes) replaced."""
    current = copy.deepcopy(CURRENT)
    *parents, last = path
    target = current
    for key in parents:
        target = target[key]
    target[last] = value
    return current


CONFIG = ("properties", "configuration")
CONTAINER = ("properties", "template", "containers", 0)
OTHER_ID = IDENTITY.replace("pdt-runner", "someone-else")


class FakeAzure:
    def __init__(self, states=("InProgress", "Succeeded"), resource=None):
        self.calls = []
        self.states = iter(states)
        self.resource = resource

    def run_quiet(self, *args, **kwargs):
        self.calls.append(args)
        if args[:3] == ("rest", "--method", "get"):
            resource = copy.deepcopy(self.resource)
            resource["properties"]["provisioningState"] = next(self.states)
            return json.dumps(resource)
        return ""

    def writes(self):
        return [call for call in self.calls if call[:3] != ("rest", "--method", "get")]


def reconcile(monkeypatch, current, fake=None, **changes):
    fake = fake or FakeAzure(resource=body(**changes))
    monkeypatch.setattr(azure, "run_quiet", fake.run_quiet)
    monkeypatch.setattr(azure.time, "sleep", lambda seconds: None)
    args = {"image": IMAGE, "cron": "0 0 * * *", "identity_id": IDENTITY, "secret_uri": SECRET,
            "storage_url": STORAGE, "fingerprint": FINGERPRINT} | changes
    azure.reconcile_job(SETTINGS, JOB, args["image"], args["cron"], args["identity_id"],
                        args["secret_uri"], args["storage_url"], current, "report",
                        args["fingerprint"])
    return fake


def test_an_unchanged_job_is_not_written(monkeypatch, capsys):
    fake = reconcile(monkeypatch, CURRENT)
    assert fake.calls == []
    assert f"Container Apps Job {JOB} is unchanged; skipped the update." in capsys.readouterr().out


@pytest.mark.parametrize("path, value", [
    (CONTAINER + ("image",), "pdtregistry.azurecr.io/report@sha256:bbb"),
    (CONTAINER + ("image",), "pdtregistry.azurecr.io/report:latest"),
    (CONFIG + ("scheduleTriggerConfig", "cronExpression"), "0 7 * * *"),
    (CONTAINER + ("resources", "cpu"), 1.0),
    (CONTAINER + ("resources", "memory"), "2Gi"),
    (CONFIG + ("replicaTimeout",), 600),
    (CONFIG + ("replicaRetryLimit",), 1),
    (CONFIG + ("scheduleTriggerConfig", "parallelism"), 2),
    (CONFIG + ("scheduleTriggerConfig", "replicaCompletionCount"), 2),
    (CONTAINER + ("env",), [{"name": "PDT_STORAGE_URL", "value": STORAGE}]),
    (CONTAINER + ("env", 2, "value"), "abfs://other/"),
    (CONFIG + ("secrets", 0, "keyVaultUrl"), SECRET + "-old"),
    (CONFIG + ("secrets", 0, "identity"), OTHER_ID),
    (CONFIG + ("secrets",), None),
    (CONFIG + ("registries", 0, "server"), "other.azurecr.io"),
    (CONFIG + ("registries", 0, "identity"), OTHER_ID),
    (CONFIG + ("registries",), []),
    (("identity", "type"), "SystemAssigned"),
    (("identity", "userAssignedIdentities"), {OTHER_ID: {}}),
    (("tags",), {"managed-by": "pdt"}),
    (("tags", "pdt-build"), "0" * 32),
])
def test_a_changed_field_is_written_in_one_patch(monkeypatch, path, value):
    fake = reconcile(monkeypatch, changed(path, value))
    [write] = fake.writes()
    assert write[:5] == ("rest", "--method", "patch", "--url", azure.job_url(SETTINGS, JOB))
    assert json.loads(write[write.index("--body") + 1]) == body()


def test_a_removed_secret_is_written_with_no_secrets_and_no_secret_env(monkeypatch):
    fake = reconcile(monkeypatch, CURRENT, secret_uri=None)
    [write] = fake.writes()
    sent = json.loads(write[write.index("--body") + 1])
    assert sent["properties"]["configuration"]["secrets"] == []
    assert sent["properties"]["template"]["containers"][0]["env"] == [
        {"name": "PDT_STORAGE_URL", "value": STORAGE}]


@pytest.mark.parametrize("path, value", [
    (CONTAINER + ("resources", "cpu"), "0.5"),
    (CONTAINER + ("resources", "memory"), "1.0Gi"),
    (CONFIG + ("registries", 0, "identity"), IDENTITY.upper()),
    (("identity", "type"), "SystemAssigned,UserAssigned"),
    (CONTAINER + ("env", 0), {"name": "PDT_ENV_JSON", "secretRef": "pdt-env", "value": ""}),
])
def test_the_same_setting_in_another_form_is_not_written(monkeypatch, path, value):
    assert reconcile(monkeypatch, changed(path, value)).calls == []


@pytest.mark.parametrize("path, value", [
    (CONTAINER + ("resources", "memory"), "1024Mi"),
    (CONTAINER + ("resources", "cpu"), "half"),
    (CONTAINER + ("resources",), {}),
])
def test_a_form_pdt_cannot_read_is_written(monkeypatch, path, value):
    assert len(reconcile(monkeypatch, changed(path, value)).writes()) == 1


def test_the_update_waits_until_azure_shows_it(monkeypatch, capsys):
    fake = FakeAzure(states=("InProgress", "InProgress", "Succeeded"), resource=body(cron="0 7 * * *"))
    reconcile(monkeypatch, CURRENT, fake, cron="0 7 * * *")
    assert [call[2] for call in fake.calls] == ["patch", "get", "get", "get"]
    assert capsys.readouterr() == ("", f"updating: Container Apps Job {JOB}\n")


def test_a_failed_update_stops_the_deploy(monkeypatch):
    fake = FakeAzure(states=("Failed",), resource=body(cron="0 7 * * *"))
    with pytest.raises(SystemExit):
        reconcile(monkeypatch, CURRENT, fake, cron="0 7 * * *")


def test_an_update_azure_never_shows_stops_with_what_differs(monkeypatch, capsys):
    fake = FakeAzure(states=["Succeeded"] * azure.JOB_UPDATE_POLLS, resource=CURRENT)
    with pytest.raises(SystemExit):
        reconcile(monkeypatch, CURRENT, fake, cron="0 7 * * *")
    out = capsys.readouterr().out
    assert "settings that differ: schedule" in out
    assert "updated: " not in out


def test_a_new_job_is_created_with_the_cli(monkeypatch):
    fake = reconcile(monkeypatch, None)
    [create] = fake.calls
    assert create[:3] == ("containerapp", "job", "create")
    assert create[create.index("--image") + 1] == IMAGE
    assert create[create.index("--env-vars") + 1:] == (
        "PDT_ENV_JSON=secretref:pdt-env", f"PDT_ENV_SECRET_RESOURCE={SECRET}",
        f"PDT_STORAGE_URL={STORAGE}")
    tags = create[create.index("--tags") + 1:create.index("--environment")]
    assert tags == ("managed-by=pdt", "pdt-app=report", f"pdt-build={FINGERPRINT}")


def test_the_job_runs_the_amd64_manifest_of_a_pushed_index(monkeypatch):
    index = {"manifests": [
        {"digest": "sha256:attestation", "platform": {"architecture": "unknown", "os": "unknown"}},
        {"digest": "sha256:amd64", "platform": {"architecture": "amd64", "os": "linux"}}]}
    monkeypatch.setattr(azure, "run_quiet", lambda *args: json.dumps(index))
    monkeypatch.setattr(azure, "az_tsv", lambda *args: pytest.fail("read the tag digest"))
    assert azure.pushed_image(SETTINGS, "report") == "pdtregistry.azurecr.io/report@sha256:amd64"


def test_the_job_runs_the_tag_digest_of_a_pushed_manifest(monkeypatch):
    monkeypatch.setattr(azure, "run_quiet", lambda *args: json.dumps({"layers": []}))
    monkeypatch.setattr(azure, "az_tsv", lambda *args: "sha256:single")
    assert azure.pushed_image(SETTINGS, "report") == "pdtregistry.azurecr.io/report@sha256:single"



def test_reconcile_returns_the_job_as_azure_holds_it_so_deploy_reads_it_no_more(monkeypatch):
    assert azure.reconcile_job(SETTINGS, JOB, IMAGE, "0 0 * * *", IDENTITY, SECRET, STORAGE,
                               CURRENT, "report", FINGERPRINT) is CURRENT
    fake = FakeAzure(resource=body(cron="0 7 * * *"))
    monkeypatch.setattr(azure, "run_quiet", fake.run_quiet)
    monkeypatch.setattr(azure.time, "sleep", lambda seconds: None)
    updated = azure.reconcile_job(SETTINGS, JOB, IMAGE, "0 7 * * *", IDENTITY, SECRET, STORAGE,
                                  CURRENT, "report", FINGERPRINT)
    assert updated["properties"]["provisioningState"] == "Succeeded"
    assert azure.normalized_job(updated) == azure.normalized_job(fake.resource)
    assert azure.reconcile_job(SETTINGS, JOB, IMAGE, "0 0 * * *", IDENTITY, SECRET, STORAGE,
                               None, "report", FINGERPRINT) is None


def test_one_wait_line_shows_from_the_patch_through_the_poll(monkeypatch, capsys, waits):
    seen = []

    class Recording(FakeAzure):
        def run_quiet(self, *args, **kwargs):
            seen.append((args[2], list(waits), capsys.readouterr().err))
            return super().run_quiet(*args, **kwargs)

    reconcile(monkeypatch, CURRENT, Recording(resource=body(cron="0 7 * * *")), cron="0 7 * * *")
    wait = [f"updating: Container Apps Job [bold not dim]{JOB}[/]"]
    assert seen == [("patch", wait, f"updating: Container Apps Job {JOB}\n"),
                    ("get", wait, ""), ("get", wait, "")]
