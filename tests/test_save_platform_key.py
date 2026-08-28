import yaml

from conftest import add_app
from pdt.config import merged_app, save_platform_key

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
