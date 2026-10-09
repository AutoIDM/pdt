import os
import threading

import pytest

from conftest import add_app
from pdt.config import is_enabled, load_yaml, merged_app, save_app_key, set_app_enabled

COMMENTED = """\
# Runs every morning.
schedule: "0 7 * * *"
enabled: true
platform:
  # which cloud
  enabled: true
"""


def test_appends_the_key_to_a_file_that_lacks_it(project):
    add_app(project, "my-report", "schedule: daily\n")
    saved = set_app_enabled("my-report", False)
    assert saved == project / "my-report" / "config.yml"
    assert saved.read_text() == "schedule: daily\nenabled: false\n"


def test_replaces_an_existing_enabled_line(project):
    add_app(project, "my-report", COMMENTED)
    text = set_app_enabled("my-report", False).read_text()
    assert text.count("\nenabled: false\n") == 1
    assert "\nenabled: true\n" not in text
    assert "  enabled: true" in text


def test_keeps_the_user_comments(project):
    add_app(project, "my-report", COMMENTED)
    text = set_app_enabled("my-report", False).read_text()
    assert "# Runs every morning." in text
    assert "  # which cloud" in text
    assert 'schedule: "0 7 * * *"' in text


def test_creates_a_missing_file(project):
    add_app(project, "my-report")
    saved = set_app_enabled("my-report", False)
    assert saved.read_text() == "enabled: false\n"


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
    assert (app_dir / "config.yml").read_text() == COMMENTED
    assert sorted(p.name for p in app_dir.iterdir() if p.name != "config.yml.lock") == before


def test_shares_the_lock_with_save_app_key(project):
    add_app(project, "my-report", COMMENTED)
    app = merged_app("my-report")
    start = threading.Barrier(2)

    def enable():
        start.wait()
        for n in range(20):
            set_app_enabled("my-report", n % 2 == 1)

    def pause():
        start.wait()
        for n in range(20):
            save_app_key(app, "pause", n % 2 == 1)

    threads = [threading.Thread(target=enable), threading.Thread(target=pause)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    saved = load_yaml(project / "my-report" / "config.yml")
    assert saved["enabled"] is True
    assert saved["pause"] is True
