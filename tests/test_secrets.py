import io
import json

from dotenv import dotenv_values

from pdt import deploy_azure, deploy_common, deploy_google_cloud

SETTINGS = {"vault": "pdt-abc"}
URI = "https://pdt-abc.vault.azure.net/secrets/pdt-demo-env"


def version(name, created, enabled=True, tags=None):
    return {"id": f"{URI}/{name}", "tags": tags,
            "attributes": {"created": created, "enabled": enabled}}


def test_secret_changes_list_every_var_grouped_by_kind_then_by_name():
    current = json.dumps({"KEEP": "1", "OLD": "hunter2", "ROTATED": "before"})
    lines = deploy_common.secret_changes(current, {"KEEP": "1", "ROTATED": "after", "NEW": "x"})
    assert lines == [("deleted", "OLD", "•••• (7 chars)", ""),
                     ("new", "NEW", "", "•••• (1 chars)"),
                     ("updated", "ROTATED", "•••• (6 chars)", "•••• (5 chars)"),
                     ("unchanged", "KEEP", "•••• (1 chars)", "•••• (1 chars)")]


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


def app_dir(tmp_path):
    return {"name": "demo", "dir": tmp_path, "platform": {"provider": "aws"}}


def test_env_line_reads_back_to_the_same_value(tmp_path):
    values = {"PLAIN": "abc-1.2/x@y=z", "SPACED": "two words #1",
              "QUOTED": 'say "hi"\\now', "MULTI": "line one\nline two", "EMPTY": ""}
    path = tmp_path / ".env"
    path.write_text("".join(deploy_common.env_line(k, v) + "\n" for k, v in values.items()))
    assert dotenv_values(path) == values


def test_diff_writes_nothing_and_names_the_save_command(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(deploy_common, "gather_secrets", lambda app: {"A": "new"})
    written = []
    code = deploy_common.run_secrets("diff", app_dir(tmp_path),
                                     json.dumps({"A": "old"}), written.append, True)
    assert code == 0 and written == []
    out = capsys.readouterr().out
    assert "updated" in out and "pdt secrets demo save" in out


def test_save_writes_the_env_values(tmp_path, monkeypatch):
    monkeypatch.setattr(deploy_common, "gather_secrets", lambda app: {"A": "new"})
    written = []
    code = deploy_common.run_secrets("save", app_dir(tmp_path),
                                     json.dumps({"A": "old"}), written.append, True)
    assert code == 0 and written == [{"A": "new"}]


def test_get_writes_a_dotenv_file_named_for_the_provider(tmp_path, monkeypatch):
    monkeypatch.setattr(deploy_common, "can_prompt", lambda interactive: False)
    code = deploy_common.run_secrets("get", app_dir(tmp_path),
                                     json.dumps({"B": "2", "A": "1"}), None, True)
    assert code == 0
    assert (tmp_path / ".env.aws").read_text() == "A=1\nB=2\n"


def test_get_uses_the_file_name_the_user_types(tmp_path, monkeypatch):
    monkeypatch.setattr(deploy_common, "can_prompt", lambda interactive: True)
    monkeypatch.setattr(deploy_common.console, "ask", lambda question, default: ".env.prod")
    deploy_common.run_secrets("get", app_dir(tmp_path), json.dumps({"A": "1"}), None, True)
    assert (tmp_path / ".env.prod").read_text() == "A=1\n"


def test_azure_set_stores_the_value_under_the_env_var_name(monkeypatch):
    from pdt import deploy_azure_container_apps as aca
    written = {}
    monkeypatch.setattr(aca, "preflight", lambda app, settings: {"identity": "id", "resource_group": "rg"})
    monkeypatch.setattr(aca, "azure_settings", lambda app: {})
    monkeypatch.setattr(aca, "secret_state", lambda *args: (True, json.dumps({"TOKEN": "old"})))
    monkeypatch.setattr(aca, "ensure_secret",
                        lambda settings, sid, values, *args: written.update(values) or URI)
    monkeypatch.setattr(aca, "az_tsv", lambda *args: "identity-id")
    monkeypatch.setattr(aca, "set_job_secret", lambda *args: None)
    monkeypatch.setattr(aca, "disable_old_secret_versions", lambda *args: None)
    monkeypatch.setattr("sys.stdin", io.StringIO("new\n"))
    assert aca.secrets({"name": "demo"}, "set", True, "TOKEN") == 0
    assert written == {"TOKEN": "new"}
