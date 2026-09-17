import json

from pdt import deploy_azure, deploy_common, deploy_google_cloud

SETTINGS = {"vault": "pdt-abc"}
URI = "https://pdt-abc.vault.azure.net/secrets/pdt-demo-env"


def version(name, created, enabled=True, tags=None):
    return {"id": f"{URI}/{name}", "tags": tags,
            "attributes": {"created": created, "enabled": enabled}}


def test_secret_changes_list_every_var_grouped_by_kind_then_by_name():
    current = json.dumps({"KEEP": "1", "OLD": "hunter2", "ROTATED": "before"})
    lines = deploy_common.secret_changes(current, {"KEEP": "1", "ROTATED": "after", "NEW": "x"})
    assert lines == [("remove", "OLD", "•••• (7 chars)", ""),
                     ("add", "NEW", "", "•••• (1 chars)"),
                     ("change", "ROTATED", "•••• (6 chars)", "•••• (5 chars)"),
                     ("same", "KEEP", "•••• (1 chars)", "")]


def test_a_long_value_shows_two_chars_at_each_end_and_a_short_one_shows_none():
    assert deploy_common.masked("sk_live_51HxYzAbCdEf") == "sk••••Ef (20 chars)"
    assert deploy_common.masked("hunter2") == "•••• (7 chars)"


def test_the_job_gets_a_secret_uri_without_a_version(monkeypatch):
    monkeypatch.setattr(deploy_azure, "az_tsv", lambda *args: f"{URI}/0123abcd")
    uri = deploy_azure.ensure_secret(SETTINGS, "pdt-demo-env", {"A": "1"}, "same", "same", "demo")
    assert uri == URI


def test_every_azure_version_but_the_latest_is_disabled(monkeypatch):
    calls = []
    versions = [version("new", "2026-03-01"), version("old", "2026-01-01"),
                version("dead", "2026-02-01", enabled=False)]
    monkeypatch.setattr(deploy_azure, "az_json", lambda *args: versions)
    monkeypatch.setattr(deploy_azure, "run_quiet",
                        lambda *args, **kwargs: calls.append(args) or "")
    deploy_azure.disable_old_secret_versions(SETTINGS, "pdt-demo-env")
    assert calls == [("keyvault", "secret", "set-attributes", "--id", f"{URI}/old",
                      "--enabled", "false")]


def test_a_version_added_in_the_portal_keeps_the_secret_owned(monkeypatch):
    versions = [version("portal", "2026-03-01"),
                version("pdt", "2026-01-01", tags={"managed-by": "pdt", "pdt-app": "demo"})]
    monkeypatch.setattr(deploy_azure, "az_json", lambda *args: versions)
    assert deploy_azure.managed_secret(SETTINGS, "pdt-demo-env", "demo")
    assert not deploy_azure.managed_secret(SETTINGS, "pdt-demo-env", "other")


def test_every_google_version_but_the_latest_is_destroyed(monkeypatch):
    calls = []
    versions = [{"name": "projects/1/secrets/pdt-demo-env/versions/3"},
                {"name": "projects/1/secrets/pdt-demo-env/versions/2"}]
    monkeypatch.setattr(deploy_google_cloud, "read_json_or_none", lambda *args: versions)
    monkeypatch.setattr(deploy_google_cloud, "run_quiet",
                        lambda *args, **kwargs: calls.append(args) or "")
    deploy_google_cloud.destroy_old_secret_versions("proj", "pdt-demo-env")
    assert calls == [("secrets", "versions", "destroy", "2", "--secret", "pdt-demo-env",
                      "--project", "proj", "--quiet")]
