"""The intune-backup example: it downloads IntuneManagement from GitHub itself,
because the module is not on the PowerShell Gallery, and runs it as a
PowerShell app with its own run.py."""

import base64
import importlib.util
import io
import tarfile

import pytest

from pdt import scaffold
from pdt.config import merged_app, powershell_scripts

EXAMPLE = scaffold.EXAMPLES / "intune-backup"


@pytest.fixture
def run_py():
    spec = importlib.util.spec_from_file_location("intune_backup_run", EXAMPLE / "run.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def release_archive(path, top="IntuneManagement-abc"):
    """A tar.gz shaped like GitHub's archive of the repository."""
    files = {
        "IntuneManagement.psd1": "@{ ModuleVersion = '4.0.0' }",
        "IntuneManagement.psm1": "",
        "Public/Start-GraphBulkExport.ps1": "",
        "Bin/MSAL_PS7/Microsoft.Identity.Client.dll": "",
        "Bin/Avalonia/Avalonia.Base.dll": "big",
        "Bin/MSAL_PS5/Microsoft.Identity.Client.dll": "",
        "UI/Avalonia/Start-Avalonia.ps1": "",
        "README.md": "",
    }
    with tarfile.open(path, "w:gz") as archive:
        for name, text in files.items():
            data = text.encode()
            info = tarfile.TarInfo(f"{top}/{name}")
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return path.as_uri()


def test_the_download_keeps_only_what_the_module_needs(run_py, tmp_path):
    folder = tmp_path / ".pdt" / "intune-management"
    run_py.fetch_module(folder, url=release_archive(tmp_path / "release.tar.gz"))
    assert (folder / "IntuneManagement.psd1").is_file()
    assert (folder / "Public" / "Start-GraphBulkExport.ps1").is_file()
    assert (folder / "Bin" / "MSAL_PS7" / "Microsoft.Identity.Client.dll").is_file()
    assert sorted(path.name for path in (folder / "Bin").iterdir()) == ["MSAL_PS7"]
    assert not (folder / "UI").exists()
    assert not (folder / "README.md").exists()
    assert (folder / "RELEASE").read_text().strip() == run_py.COMMIT


def test_the_pinned_release_downloads_once(run_py, tmp_path):
    folder = tmp_path / "intune-management"
    run_py.fetch_module(folder, url=release_archive(tmp_path / "release.tar.gz"))
    # A second run finds the release in place and does not reach the URL.
    run_py.fetch_module(folder, url=(tmp_path / "gone.tar.gz").as_uri())
    assert (folder / "IntuneManagement.psd1").is_file()


def test_another_release_replaces_the_one_on_disk(run_py, tmp_path):
    folder = tmp_path / "intune-management"
    folder.mkdir()
    (folder / "RELEASE").write_text("an-older-commit\n")
    (folder / "Old.ps1").write_text("")
    run_py.fetch_module(folder, url=release_archive(tmp_path / "release.tar.gz"))
    assert not (folder / "Old.ps1").exists()
    assert (folder / "RELEASE").read_text().strip() == run_py.COMMIT


def test_a_failed_download_names_the_url(run_py, tmp_path, capsys):
    url = (tmp_path / "missing.tar.gz").as_uri()
    with pytest.raises(SystemExit) as exit_info:
        run_py.fetch_module(tmp_path / "intune-management", url=url)
    assert exit_info.value.code == run_py.EXIT_DOWNLOAD
    out = capsys.readouterr().out
    assert "could not download IntuneManagement" in out and url in out


def test_the_pin_is_a_full_commit_of_the_github_repository(run_py):
    assert len(run_py.COMMIT) == 40 and int(run_py.COMMIT, 16) >= 0
    assert run_py.ARCHIVE.format(commit=run_py.COMMIT).startswith(
        "https://github.com/Micke-K/IntuneManagement/archive/")


def test_a_certificate_in_b64_becomes_a_file(run_py, project, tmp_path, monkeypatch):
    monkeypatch.setenv("PDT_AZURE_PRIVATE_KEY_B64", base64.b64encode(b"pfx bytes").decode())
    monkeypatch.setenv("PDT_AZURE_PRIVATE_KEY_PATH", "ignored.pfx")
    env = run_py.certificate_env(project, tmp_path)
    assert (tmp_path / "certificate.pfx").read_bytes() == b"pfx bytes"
    assert env == {"PDT_AZURE_PRIVATE_KEY_PATH": str(tmp_path / "certificate.pfx")}


def test_a_relative_certificate_path_is_found_in_the_project(run_py, project, monkeypatch):
    app = project / "intune-backup"
    app.mkdir()
    (project / "intune-backup.pfx").write_bytes(b"pfx")
    monkeypatch.delenv("PDT_AZURE_PRIVATE_KEY_B64", raising=False)
    monkeypatch.setenv("PDT_AZURE_PRIVATE_KEY_PATH", "intune-backup.pfx")
    env = run_py.certificate_env(app, project)
    assert env == {"PDT_AZURE_PRIVATE_KEY_PATH": str(project / "intune-backup.pfx")}


def test_a_secret_needs_no_certificate_file(run_py, project, monkeypatch):
    monkeypatch.delenv("PDT_AZURE_PRIVATE_KEY_B64", raising=False)
    monkeypatch.delenv("PDT_AZURE_PRIVATE_KEY_PATH", raising=False)
    assert run_py.certificate_env(project, project) == {}


def test_it_is_a_powershell_app_with_its_own_run_py(project):
    assert scaffold.new_app("intune-backup", "intune-backup") == 0
    app_dir = project / "intune-backup"
    # requirements.psd1 next to run.py makes run.py the job of a PowerShell app.
    assert (app_dir / "run.py").is_file() and (app_dir / "requirements.psd1").is_file()
    assert powershell_scripts(app_dir) == ["run.ps1"]
    assert merged_app("intune-backup")["env"]["required"] == ["PDT_AZURE_TENANT_ID", "PDT_AZURE_CLIENT_ID"]
