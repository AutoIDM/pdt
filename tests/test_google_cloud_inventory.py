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
