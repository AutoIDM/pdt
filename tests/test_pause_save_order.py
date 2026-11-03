import pytest

from conftest import add_app
from pdt import config, deploy


@pytest.fixture
def deployed(project, monkeypatch):
    add_app(project, "my-report", "schedule: daily\npause: false\n")
    monkeypatch.setattr(config, "is_deployed", lambda name: True)
    monkeypatch.setattr(deploy.lifecycle, "emit", lambda *args, **kwargs: None)
    return project / "my-report" / "config.yml"


def test_a_pause_the_cloud_refused_leaves_config_yml_as_it_was(deployed, monkeypatch):
    monkeypatch.setattr(deploy, "dispatch", lambda *args, **kwargs: 1)
    assert deploy.pause("my-report", True) == 1
    assert deployed.read_text() == "schedule: daily\npause: false\n"


def test_config_yml_is_saved_after_the_cloud_change(deployed, monkeypatch):
    seen = []
    monkeypatch.setattr(deploy, "dispatch",
                        lambda *args, **kwargs: seen.append(deployed.read_text()) or 0)
    assert deploy.pause("my-report", True) == 0
    assert seen == ["schedule: daily\npause: false\n"]
    assert deployed.read_text() == "schedule: daily\npause: true\n"


def test_an_app_that_is_not_deployed_only_saves_the_key(project, monkeypatch):
    add_app(project, "my-report", "schedule: daily\n")
    monkeypatch.setattr(config, "is_deployed", lambda name: False)
    monkeypatch.setattr(deploy, "dispatch", lambda *args, **kwargs: pytest.fail("dispatched"))
    monkeypatch.setattr(deploy.lifecycle, "emit", lambda *args, **kwargs: None)
    assert deploy.pause("my-report", True) == 0
    assert (project / "my-report" / "config.yml").read_text() == "schedule: daily\npause: true\n"
