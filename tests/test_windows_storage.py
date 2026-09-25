import pytest

from conftest import add_app
from pdt import config, deploy_windows


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.delenv("PDT_PROJECT", raising=False)
    (tmp_path / "pdt.yml").write_text("platform:\n  provider: windows\n")
    monkeypatch.setenv("ProgramData", str(tmp_path / "ProgramData"))
    monkeypatch.chdir(tmp_path)
    return tmp_path


def windows_app(project, monkeypatch):
    add_app(project, "my-report", "schedule: hourly\ntimezone: local\n")
    monkeypatch.setattr(deploy_windows, "_preflight",
                        lambda require_uv=True: ("powershell.exe", "uv.exe"))
    monkeypatch.setattr(deploy_windows, "_task_state", lambda powershell, name: "absent")
    monkeypatch.setattr(deploy_windows, "_deploying_user", lambda: r"PC\jon")
    return config.merged_app("my-report")


def test_deploy_plans_the_storage_folder(project, monkeypatch, capsys):
    app = windows_app(project, monkeypatch)
    assert deploy_windows.deploy(app, assume_yes=False) == 1
    folder = project / "ProgramData" / "pdt" / "my-report" / "storage"
    out = capsys.readouterr().out
    assert f"use folder {folder} for the app's files (kept after destroy)" in out


def test_destroy_reports_the_kept_folder(project, monkeypatch, capsys):
    app = windows_app(project, monkeypatch)
    folder = project / "ProgramData" / "pdt" / "my-report" / "storage"
    folder.mkdir(parents=True)
    (folder / "a.csv").write_text("a")
    assert deploy_windows.destroy(app, assume_yes=True) == 0
    out = capsys.readouterr().out
    assert f"kept: folder {folder} (1 files)" in out


def test_storage_ls_lists_the_folder_contents(project, monkeypatch, capsys):
    app = windows_app(project, monkeypatch)
    folder = project / "ProgramData" / "pdt" / "my-report" / "storage"
    folder.mkdir(parents=True)
    (folder / "a.csv").write_text("a")
    (folder / "b.csv").write_text("b")
    assert deploy_windows.storage(app, ["ls"], False) == 0
    out = capsys.readouterr().out
    assert "a.csv" in out
    assert "b.csv" in out
