import os
import threading

import pytest
import yaml

from conftest import add_app
from pdt import config
from pdt.config import load_yaml, merged_app, save_platform_key

COMMENTED = """\
# The top of my project.
platform:
  # which cloud
  provider: aws
  region: us-east-1

apps: []
"""


def test_adds_the_key_to_the_project_file(project):
    (project / "pdt.yml").write_text(COMMENTED)
    add_app(project, "my-report")
    saved = save_platform_key(merged_app("my-report"), "account", "123456789012")
    assert saved == project / "pdt.yml"
    assert yaml.safe_load(saved.read_text())["platform"]["account"] == "123456789012"


def test_keeps_the_user_comments(project):
    (project / "pdt.yml").write_text(COMMENTED)
    add_app(project, "my-report")
    saved = save_platform_key(merged_app("my-report"), "account", "123456789012")
    text = saved.read_text()
    assert "# The top of my project." in text
    assert "# which cloud" in text


def test_replaces_a_value_that_is_already_there(project):
    (project / "pdt.yml").write_text(COMMENTED)
    add_app(project, "my-report")
    saved = save_platform_key(merged_app("my-report"), "region", "eu-west-1")
    assert yaml.safe_load(saved.read_text())["platform"]["region"] == "eu-west-1"
    assert saved.read_text().count("region:") == 1


def test_creates_the_platform_block_when_there_is_none(project):
    (project / "pdt.yml").write_text("apps: []\n")
    add_app(project, "my-report", "platform:\n  provider: aws\n")
    saved = save_platform_key(merged_app("my-report"), "account", "123456789012")
    assert yaml.safe_load(saved.read_text())["platform"]["account"] == "123456789012"


def test_writes_to_the_app_file_when_the_app_overrides_the_provider(project):
    (project / "pdt.yml").write_text("platform:\n  provider: azure\n")
    add_app(project, "my-report", "platform:\n  provider: aws\n  region: us-east-1\n")
    saved = save_platform_key(merged_app("my-report"), "account", "123456789012")
    assert saved == project / "my-report" / "config.yml"
    assert yaml.safe_load((project / "pdt.yml").read_text())["platform"] == {"provider": "azure"}


def test_the_saved_value_comes_back_through_merged_app(project):
    (project / "pdt.yml").write_text(COMMENTED)
    add_app(project, "my-report")
    save_platform_key(merged_app("my-report"), "account", "123456789012")
    assert merged_app("my-report")["platform"]["account"] == "123456789012"


def test_a_leading_zero_survives(project):
    # Unquoted, yaml reads an account id back as an int and drops the zero.
    (project / "pdt.yml").write_text(COMMENTED)
    add_app(project, "my-report")
    saved = save_platform_key(merged_app("my-report"), "account", "012345678901")
    assert yaml.safe_load(saved.read_text())["platform"]["account"] == "012345678901"
    assert merged_app("my-report")["platform"]["account"] == "012345678901"


def test_saving_twice_leaves_one_key(project):
    (project / "pdt.yml").write_text(COMMENTED)
    add_app(project, "my-report")
    save_platform_key(merged_app("my-report"), "account", "111111111111")
    saved = save_platform_key(merged_app("my-report"), "account", "222222222222")
    assert saved.read_text().count("account:") == 1
    assert yaml.safe_load(saved.read_text())["platform"]["account"] == "222222222222"


def test_a_value_with_quotes_backslashes_and_newlines_reads_back_unchanged(project):
    (project / "pdt.yml").write_text(COMMENTED)
    add_app(project, "my-report")
    value = 'say "hi"\\there\nnext line'
    saved = save_platform_key(merged_app("my-report"), "profile", value)
    assert load_yaml(saved)["platform"]["profile"] == value


def test_a_failed_write_leaves_the_file_as_it_was(project, monkeypatch):
    (project / "pdt.yml").write_text(COMMENTED)
    add_app(project, "my-report")
    app = merged_app("my-report")
    before = sorted(p.name for p in project.iterdir())

    def fail(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", fail)
    with pytest.raises(OSError):
        save_platform_key(app, "account", "123456789012")
    assert (project / "pdt.yml").read_text() == COMMENTED
    assert not [p.name for p in project.iterdir() if p.name.endswith(".tmp")]
    assert sorted(p.name for p in project.iterdir() if p.name != "pdt.yml.lock") == before


def test_two_writers_at_once_both_land(project):
    (project / "pdt.yml").write_text(COMMENTED)
    add_app(project, "my-report")
    app = merged_app("my-report")
    start = threading.Barrier(2)

    def write(key):
        start.wait()
        for n in range(20):
            save_platform_key(app, key, f"{key}-{n}")

    threads = [threading.Thread(target=write, args=(key,)) for key in ("account", "profile")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    platform = load_yaml(project / "pdt.yml")["platform"]
    assert platform["account"] == "account-19"
    assert platform["profile"] == "profile-19"


def test_write_text_atomically_leaves_only_the_target(tmp_path):
    target = tmp_path / "pdt.yml"
    target.write_text("old\n")
    config.write_text_atomically(target, "new\n")
    assert target.read_text() == "new\n"
    assert [p.name for p in tmp_path.iterdir()] == ["pdt.yml"]
