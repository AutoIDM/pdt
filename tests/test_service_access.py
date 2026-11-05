import os
import subprocess

import pytest

from pdt import service_access


@pytest.mark.parametrize("folder", ["__pycache__", ".venv", ".git", ".pdt"])
def test_a_writable_file_in_a_tool_folder_is_found(tmp_path, monkeypatch, folder):
    planted = tmp_path / "hr" / folder / "helper.pyc"
    planted.parent.mkdir(parents=True)
    planted.write_bytes(b"")
    monkeypatch.setattr(service_access, "posix_writer",
                        lambda path: "each user" if path == planted else None)
    assert service_access.posix_writable(tmp_path) == ("each user", planted)


def test_a_posix_walk_stopped_by_the_limit_refuses(tmp_path, monkeypatch):
    for name in ("a", "b", "c"):
        (tmp_path / name).write_text("")
    monkeypatch.setattr(service_access, "posix_writer", lambda path: None)
    monkeypatch.setattr(service_access, "WALK_LIMIT", 2)
    with pytest.raises(service_access.AccessError, match="more than 2"):
        service_access.posix_writable(tmp_path)


def test_a_windows_walk_stopped_by_the_limit_refuses(tmp_path, monkeypatch):
    monkeypatch.setattr(service_access, "run_powershell",
                        lambda script, env: f"\t{tmp_path / 'z'}\r\n")
    with pytest.raises(service_access.AccessError, match="more than 50000"):
        service_access.windows_writable(tmp_path)


@pytest.mark.skipif(os.name != "nt", reason="reads Windows access lists")
def test_a_file_deep_in_a_project_with_its_own_write_right_is_found(tmp_path):
    project = tmp_path / "example"
    (project / "hr").mkdir(parents=True)
    (project / "hr" / "run.py").write_text("print('hi')\n")
    for args in (["/setowner", "*S-1-5-32-544", "/T"],
                 ["/inheritance:r", "/grant:r", "*S-1-5-18:(OI)(CI)F", "*S-1-5-32-544:(OI)(CI)F",
                  "/T"]):
        subprocess.run(["icacls", str(project), *args], capture_output=True, check=True)
    assert service_access.writable_by_others(project) is None
    subprocess.run(["icacls", str(project / "hr" / "run.py"), "/grant", "*S-1-5-32-545:(M)"],
                   capture_output=True, check=True)
    account, path = service_access.writable_by_others(project)
    assert path == project / "hr" / "run.py"
    assert account.endswith("Users")
