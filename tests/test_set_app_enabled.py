import os

import pytest

from conftest import add_app
from pdt.config import is_enabled, set_app_enabled

COMMENTED = """\
# Runs every morning.
schedule: "0 7 * * *"
enabled: true
platform:
  # which cloud
  enabled: true
"""


def test_the_saved_value_comes_back_through_is_enabled(project):
    (project / "pdt.yml").write_text("platform:\n  provider: azure\napps:\n  - name: my-report\n    enabled: true\n")
    add_app(project, "my-report", COMMENTED)
    set_app_enabled("my-report", False)
    assert not is_enabled("my-report")
    set_app_enabled("my-report", True)
    assert is_enabled("my-report")


def test_a_failed_write_leaves_the_file_as_it_was(project, monkeypatch):
    add_app(project, "my-report", COMMENTED)
    app_dir = project / "my-report"
    before = sorted(p.name for p in app_dir.iterdir())

    def fail(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", fail)
    with pytest.raises(OSError):
        set_app_enabled("my-report", False)
    assert (app_dir / "pdt.yml").read_text() == COMMENTED
    assert sorted(p.name for p in app_dir.iterdir() if p.name != "pdt.yml.lock") == before
