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


def asset_index(monkeypatch, *names):
    def gcloud(*args):
        if args[:2] == ("asset", "search-all-resources"):
            return [{"assetType": "secretmanager.googleapis.com/Secret",
                     "name": f"//secretmanager.googleapis.com/projects/verify/secrets/{name}",
                     "labels": {"managed-by": "pdt"}} for name in names]
        return []

    monkeypatch.setattr(inventory, "gcloud", gcloud)


def test_a_secret_the_search_index_has_not_dropped_yet_is_not_a_leftover(monkeypatch):
    # The index lags minutes behind a delete, so Secret Manager decides.
    asset_index(monkeypatch, "pdt-app-one-env")
    monkeypatch.setattr(inventory, "gcloud_or_none", lambda *args: None)
    assert inventory.google_cloud_inventory(SETTINGS) == []


def test_a_secret_secret_manager_confirms_is_a_leftover(monkeypatch):
    asset_index(monkeypatch, "pdt-app-one-env")
    asked = []

    def gcloud_or_none(*args):
        asked.append(args)
        return {"name": "projects/verify/secrets/pdt-app-one-env"}

    monkeypatch.setattr(inventory, "gcloud_or_none", gcloud_or_none)
    found = inventory.google_cloud_inventory(SETTINGS)
    assert [resource.name for resource in found] == ["pdt-app-one-env"]
    assert asked == [("secrets", "describe", "pdt-app-one-env", "--project", "verify")]


def test_a_cloud_run_job_is_confirmed_in_the_region_the_index_names(monkeypatch):
    def gcloud(*args):
        if args[:2] == ("asset", "search-all-resources"):
            return [{"assetType": "run.googleapis.com/Job", "location": "europe-west1",
                     "name": "//run.googleapis.com/projects/verify/jobs/pdt-app-one",
                     "labels": {"managed-by": "pdt"}}]
        return []

    asked = []

    def gcloud_or_none(*args):
        asked.append(args)
        return {"name": "pdt-app-one"}

    monkeypatch.setattr(inventory, "gcloud", gcloud)
    monkeypatch.setattr(inventory, "gcloud_or_none", gcloud_or_none)
    found = inventory.google_cloud_inventory(SETTINGS)
    assert [resource.name for resource in found] == ["pdt-app-one"]
    assert asked == [("run", "jobs", "describe", "pdt-app-one",
                      "--region", "europe-west1", "--project", "verify")]
