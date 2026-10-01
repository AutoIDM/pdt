import inventory

SETTINGS = {"project": "verify", "region": "us-central1"}


def test_the_account_the_run_signs_in_with_is_not_a_leftover(monkeypatch):
    def gcloud(*args):
        if args[:3] == ("config", "get-value", "account"):
            return "pdt-ci@verify.iam.gserviceaccount.com"
        if args[:3] == ("iam", "service-accounts", "list"):
            return [
                {"name": "projects/verify/serviceAccounts/pdt-ci@verify.iam.gserviceaccount.com",
                 "email": "pdt-ci@verify.iam.gserviceaccount.com", "displayName": "CI"},
                {"name": "projects/verify/serviceAccounts/pdt-runner@verify.iam.gserviceaccount.com",
                 "email": "pdt-runner@verify.iam.gserviceaccount.com",
                 "displayName": "pdt job runner"},
            ]
        return []

    monkeypatch.setattr(inventory, "gcloud", gcloud)
    found = inventory.google_cloud_inventory(SETTINGS)
    assert [resource.name for resource in found] == ["pdt-runner"]
    assert found[0].tags == inventory.MANAGED


def asset_search(monkeypatch, *assets):
    def gcloud(*args):
        if args[:2] == ("asset", "search-all-resources"):
            return list(assets)
        return []

    monkeypatch.setattr(inventory, "gcloud", gcloud)


SECRET = {"assetType": "secretmanager.googleapis.com/Secret",
          "name": "//secretmanager.googleapis.com/projects/verify/secrets/pdt-app-one-env",
          "labels": {"managed-by": "pdt"}}
JOB = {"assetType": "run.googleapis.com/Job", "location": "europe-west1",
       "name": "//run.googleapis.com/projects/verify/locations/europe-west1/jobs/pdt-app-one",
       "labels": {"managed-by": "pdt"}}


def test_an_asset_its_service_says_is_gone_is_dropped(monkeypatch):
    asset_search(monkeypatch, SECRET, JOB)
    errors = {
        "secrets": "ERROR: (gcloud.secrets.describe) NOT_FOUND: Secret [x] not found.",
        "run": "ERROR: (gcloud.run.jobs.describe) Cannot find job [pdt-app-one].",
    }
    monkeypatch.setattr(inventory, "gcloud_error", lambda *args: errors[args[0]])
    assert inventory.google_cloud_inventory(SETTINGS) == []


def test_an_asset_its_service_confirms_is_kept(monkeypatch):
    asset_search(monkeypatch, SECRET, JOB)
    asked = []
    monkeypatch.setattr(inventory, "gcloud_error", lambda *args: asked.append(args) or "")
    found = inventory.google_cloud_inventory(SETTINGS)
    assert [(resource.name, resource.note) for resource in found] == [
        ("pdt-app-one-env", ""), ("pdt-app-one", "")]
    assert asked == [
        ("secrets", "describe", "pdt-app-one-env", "--project", "verify"),
        ("run", "jobs", "describe", "pdt-app-one", "--region", "europe-west1",
         "--project", "verify"),
    ]


def test_an_asset_whose_describe_fails_otherwise_is_kept_with_the_error(monkeypatch):
    asset_search(monkeypatch, SECRET)
    denied = "ERROR: (gcloud.secrets.describe) PERMISSION_DENIED: Permission denied."
    monkeypatch.setattr(inventory, "gcloud_error", lambda *args: denied)
    found = inventory.google_cloud_inventory(SETTINGS)
    assert [resource.name for resource in found] == ["pdt-app-one-env"]
    assert found[0].note == f"describe failed: {denied}"
