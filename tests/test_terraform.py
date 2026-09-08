import json
import hashlib
import io
import os
import zipfile

import pytest

from pdt import terraform


def test_configuration_pins_provider_and_keeps_optional_blocks():
    result = terraform.configuration(
        "google", {"project": "example"}, {"google_storage_bucket": {}},
        data={"google_client_config": {"current": {}}},
        outputs={"project": {"value": "example"}})

    assert result["terraform"]["required_version"] == "= 1.16.1"
    assert result["terraform"]["required_providers"]["google"] == {
        "source": "hashicorp/google", "version": "= 8.1.0"}
    assert result["data"]["google_client_config"] == {"current": {}}
    assert result["output"]["project"]["value"] == "example"


def test_installer_verifies_archive_and_extracts_only_the_executable(tmp_path, monkeypatch):
    binary_name = "terraform.exe" if os.name == "nt" else "terraform"
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as package:
        package.writestr(binary_name, b"binary")
        package.writestr("../outside", b"unwanted")
    raw = archive.getvalue()
    monkeypatch.setattr(terraform.shutil, "which", lambda _name: None)
    monkeypatch.setattr(terraform, "data_directory", lambda: tmp_path / "tools")
    monkeypatch.setattr(terraform.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(terraform.platform, "machine", lambda: "arm64")
    monkeypatch.setitem(terraform.CHECKSUMS, "darwin_arm64", hashlib.sha256(raw).hexdigest())
    monkeypatch.setattr(terraform.urllib.request, "urlopen", lambda *_args, **_kwargs: io.BytesIO(raw))

    executable = terraform.ensure_terraform(assume_yes=True)

    assert executable == str(tmp_path / "tools" / binary_name)
    assert (tmp_path / "tools" / binary_name).read_bytes() == b"binary"
    assert not (tmp_path / "outside").exists()


def test_installer_rejects_an_unverified_archive(tmp_path, monkeypatch):
    monkeypatch.setattr(terraform.shutil, "which", lambda _name: None)
    monkeypatch.setattr(terraform, "data_directory", lambda: tmp_path / "tools")
    monkeypatch.setattr(terraform.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(terraform.platform, "machine", lambda: "arm64")
    monkeypatch.setattr(terraform.urllib.request, "urlopen", lambda *_args, **_kwargs: io.BytesIO(b"wrong archive"))

    with pytest.raises(terraform.TerraformError, match="checksum"):
        terraform.ensure_terraform(assume_yes=True)

    assert not (tmp_path / "tools" / ("terraform.exe" if os.name == "nt" else "terraform")).exists()


def test_windows_arm_uses_amd64_terraform_for_cloud_provider_support(tmp_path, monkeypatch):
    monkeypatch.setattr(terraform.shutil, "which", lambda _name: None)
    monkeypatch.setattr(terraform, "data_directory", lambda: tmp_path / "tools")
    monkeypatch.setattr(terraform.platform, "system", lambda: "Windows")
    monkeypatch.setattr(terraform.platform, "machine", lambda: "ARM64")
    urls = []
    monkeypatch.setattr(terraform.urllib.request, "urlopen", lambda url, **_kwargs: urls.append(url) or io.BytesIO(b"test"))

    with pytest.raises(terraform.TerraformError, match="checksum"):
        terraform.ensure_terraform(assume_yes=True)

    assert urls[0].endswith("_windows_amd64.zip")


@pytest.mark.skipif(os.environ.get("PDT_RUN_TERRAFORM_SMOKE") != "1",
                    reason="set PDT_RUN_TERRAFORM_SMOKE=1 to run the local Terraform smoke test")
def test_workspace_reconciles_builtin_terraform_data(tmp_path):
    configuration = {"resource": {"terraform_data": {"task": {"input": "first"}}}}
    workspace = terraform.TerraformWorkspace(tmp_path, configuration, assume_yes=True)

    first = workspace.plan()
    assert first.actions == ["create terraform_data.task"]
    workspace.apply(first)

    unchanged = workspace.plan()
    assert unchanged.actions == []
    workspace.apply(unchanged)

    workspace.configuration["resource"]["terraform_data"]["task"]["input"] = "second"
    workspace.init()
    update = workspace.plan()
    assert update.actions == ["update terraform_data.task"]
    workspace.apply(update)

    destroy = workspace.plan(destroy=True)
    assert destroy.actions == ["delete terraform_data.task"]
    workspace.apply(destroy)
    state = json.loads(workspace.run("show", "-json").stdout)
    assert state.get("values") is None


def test_import_skips_addresses_already_in_state(tmp_path, monkeypatch):
    workspace = terraform.TerraformWorkspace.__new__(terraform.TerraformWorkspace)
    workspace.directory = tmp_path
    workspace.initialized = True
    calls = []

    class Result:
        def __init__(self, stdout=""):
            self.stdout = stdout

    def run(*args, **_kwargs):
        calls.append(args)
        if args == ("state", "list"):
            return Result("terraform_data.present\n")
        return Result()

    monkeypatch.setattr(workspace, "run", run)
    workspace.import_resources({"terraform_data.present": "old",
                                "terraform_data.absent": "new"})

    assert calls == [("state", "list"),
                     ("import", "-input=false", "-no-color", "terraform_data.absent", "new")]


def test_apply_uses_the_saved_plan_and_removes_it(tmp_path, monkeypatch):
    workspace = terraform.TerraformWorkspace.__new__(terraform.TerraformWorkspace)
    plan_path = tmp_path / "saved.tfplan"
    plan_path.write_text("plan")
    calls = []
    monkeypatch.setattr(workspace, "run", lambda *args, **_kwargs: calls.append(args))

    workspace.apply(terraform.Plan(plan_path, ["create terraform_data.task"]))

    assert calls == [("apply", "-input=false", "-no-color", str(plan_path))]
    assert not plan_path.exists()


def test_runner_redacts_tokens_from_failures(tmp_path, monkeypatch):
    workspace = terraform.TerraformWorkspace.__new__(terraform.TerraformWorkspace)
    workspace.binary = "terraform"
    workspace.directory = tmp_path
    workspace.env = {"GOOGLE_OAUTH_ACCESS_TOKEN": "this-is-a-secret-token"}

    result = type("Result", (), {"returncode": 1, "stderr": "this-is-a-secret-token failed", "stdout": ""})()
    monkeypatch.setattr(terraform.subprocess, "run", lambda *args, **_kwargs: result)

    with pytest.raises(terraform.TerraformError) as exc:
        workspace.run("plan")

    assert "this-is-a-secret-token" not in str(exc.value)
    assert "[redacted]" in str(exc.value)
