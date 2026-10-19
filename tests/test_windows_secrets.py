import json
import re
import shutil

import pytest

from conftest import add_app
from pdt import config, deploy_windows


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.delenv("PDT_PROJECT", raising=False)
    (tmp_path / "pdt.yml").write_text("platform:\n  provider: windows\n")
    monkeypatch.setenv("ProgramData", str(tmp_path / "ProgramData"))
    monkeypatch.chdir(tmp_path)
    add_app(tmp_path, "my-report", "schedule: hourly\ntimezone: local\nenv:\n  required: [API_KEY]\n")
    monkeypatch.setattr(deploy_windows, "_preflight", lambda: "powershell.exe")
    return config.merged_app("my-report")


def deployed_secret(values: dict) -> None:
    secret = deploy_windows.secrets_file("my-report")
    secret.parent.mkdir(parents=True, exist_ok=True)
    secret.write_text(json.dumps(values))


def fake_elevation(monkeypatch, admin: bool):
    scripts = []

    def run(powershell, script, **kwargs):
        scripts.append((script, kwargs))
        copy = re.match(r"Copy-Item -Path '([^']+)' -Destination '([^']+)'", script)
        if copy:
            shutil.copy(copy.group(1), copy.group(2))
        move = re.search(r"ReadAllBytes\('([^']+)'\)", script)
        if move:
            shutil.move(move.group(1), deploy_windows.secrets_file("my-report"))
        return True

    monkeypatch.setattr(deploy_windows, "_is_admin", lambda: admin)
    monkeypatch.setattr(deploy_windows, "_run", run)
    return scripts


def test_get_before_the_first_deploy_says_to_deploy(app, monkeypatch, capsys):
    fake_elevation(monkeypatch, admin=True)
    with pytest.raises(SystemExit):
        deploy_windows.secrets(app, "get", True)
    assert "has no deployed secrets yet" in capsys.readouterr().out


def test_an_elevated_shell_reads_the_secret_without_a_uac_prompt(app, monkeypatch, capsys):
    deployed_secret({"API_KEY": "old-value-123456"})
    monkeypatch.setenv("API_KEY", "old-value-123456")
    scripts = fake_elevation(monkeypatch, admin=True)
    assert deploy_windows.secrets(app, "diff", True) == 0
    assert scripts == []
    assert "already match" in capsys.readouterr().out


def test_a_shell_that_is_not_elevated_copies_the_secret_once_through_uac(app, monkeypatch):
    deployed_secret({"API_KEY": "old-value-123456"})
    monkeypatch.setenv("API_KEY", "new-value-654321")
    scripts = fake_elevation(monkeypatch, admin=False)
    assert deploy_windows.secrets(app, "diff", True) == 0
    (script, kwargs), = scripts
    assert script.startswith(f"Copy-Item -Path '{deploy_windows.secrets_file('my-report')}'")
    assert kwargs == {"elevate": True}


def test_save_moves_the_new_values_in_and_locks_them_to_admins(app, monkeypatch):
    deployed_secret({"API_KEY": "old-value-123456"})
    monkeypatch.setenv("API_KEY", "new-value-654321")
    scripts = fake_elevation(monkeypatch, admin=True)
    assert deploy_windows.secrets(app, "save", True) == 0
    (script, kwargs), = scripts
    assert (script.index("New-Item -ItemType File -Path $placed") < script.index("Set-PdtAcl $placed $false")
            < script.index("[IO.File]::WriteAllBytes($placed")
            < script.index("[IO.File]::Replace($placed, $secret, [NullString]::Value)")
            < script.index("Set-PdtAcl $secret $false") < script.index("} catch { Remove-Item -Force $placed"))
    assert "$null" not in script
    assert "icacls" not in script
    assert kwargs == {"elevate": True}
    saved = deploy_windows.secrets_file("my-report").read_text()
    assert json.loads(saved) == {"API_KEY": "new-value-654321"}


def test_destroy_removes_the_copy_and_the_secrets_and_keeps_storage(app, monkeypatch, capsys):
    folder = deploy_windows.app_folder("my-report")
    for part in ("app", "runtime", "logs", "storage"):
        (folder / part).mkdir(parents=True)
    deployed_secret({"API_KEY": "x"})
    scripts = []
    monkeypatch.setattr(deploy_windows, "_task_state", lambda powershell, name: "managed")
    monkeypatch.setattr(deploy_windows, "_run",
                        lambda powershell, script, **kw: scripts.append(script) or True)

    assert deploy_windows.destroy(app, assume_yes=True) == 0

    (script,) = scripts
    for part in ("app", "runtime", "logs", "secrets.json"):
        assert f"Remove-Item -Recurse -Force -Path '{folder / part}'" in script
    assert str(folder / "storage") not in script
    out = capsys.readouterr().out
    assert f"removed: secrets file {folder / 'secrets.json'}" in out


def test_destroy_also_removes_copies_a_failed_deploy_left(app, monkeypatch, capsys):
    folder = deploy_windows.app_folder("my-report")
    (folder / "app.new").mkdir(parents=True)
    (folder / "runtime.old").mkdir()
    (folder / "secrets.json.tmp").write_text("{}")
    scripts = []
    monkeypatch.setattr(deploy_windows, "_task_state", lambda powershell, name: "absent")
    monkeypatch.setattr(deploy_windows, "_run",
                        lambda powershell, script, **kw: scripts.append(script) or True)
    assert deploy_windows.destroy(app, assume_yes=True) == 0
    (script,) = scripts
    assert f"Remove-Item -Recurse -Force -Path '{folder / 'app.new'}'" in script
    assert f"Remove-Item -Recurse -Force -Path '{folder / 'runtime.old'}'" in script
    assert f"Remove-Item -Recurse -Force -Path '{folder / 'secrets.json.tmp'}'" in script
    assert "removed: deployed app folder (new copy)" in capsys.readouterr().out
