import os
import subprocess

import pytest

from pdt import service_access


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
