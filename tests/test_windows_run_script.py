import pytest

from conftest import add_app
from pdt import config, deploy_windows

DEPLOY_UV = "/Users/visch/AppData/Local/Microsoft/WinGet/Links/uv.exe"


@pytest.fixture
def script(tmp_path, monkeypatch):
    monkeypatch.delenv("PDT_PROJECT", raising=False)
    (tmp_path / "pdt.yml").write_text("platform:\n  provider: windows\n")
    monkeypatch.chdir(tmp_path)
    add_app(tmp_path, "my-report", "schedule: hourly\ntimezone: local\n")
    return deploy_windows._run_script(config.merged_app("my-report"), DEPLOY_UV)


def test_uv_is_looked_up_on_the_path_before_the_fallback(script):
    lookup = script.index("$uv = (Get-Command uv.exe, uv -ErrorAction SilentlyContinue")
    fallback = script.index(f"$uv = '{DEPLOY_UV}'")
    assert script.index("$log = ") < lookup < fallback


def test_fallback_is_the_deploy_time_path(script):
    assert f"if (-not $uv -or -not (Test-Path $uv)) {{ $uv = '{DEPLOY_UV}' }}" in script


def test_missing_uv_logs_exit_127(script):
    missing = script[script.index("if (-not (Test-Path $uv)) {"):]
    missing = missing[:missing.index("exit 127 }") + len("exit 127 }")]
    assert "uv was not found on the PATH or at $uv" in missing
    assert "'pdt: exit 127' | Out-File -Encoding utf8 -Append -FilePath $log" in missing


def test_app_runs_with_the_resolved_uv(script):
    assert "& $uv run --script run.py" in script
    assert f"'{DEPLOY_UV}' run --script" not in script
    assert script.count(DEPLOY_UV) == 1
