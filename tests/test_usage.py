import argparse
import contextlib
import json

import pytest

from conftest import add_app
from pdt import cli, deploy, settings, usage

EXAMPLES = argparse.Namespace(command="examples")


def run_cli(monkeypatch, *argv):
    monkeypatch.setattr("sys.argv", ["pdt", *argv])
    return cli.main()


@pytest.fixture
def sent(monkeypatch):
    monkeypatch.delenv("DO_NOT_TRACK")
    monkeypatch.setattr(usage, "USAGE_KEY", "test-key")
    bodies = []

    def fake_urlopen(request, timeout):
        assert timeout == 2
        bodies.append(json.loads(request.data))
        return contextlib.nullcontext()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    return bodies


def interactive(monkeypatch, value):
    monkeypatch.setattr(usage, "can_prompt", lambda _interactive: value)


def test_notice_prints_once_and_creates_the_file(sent, monkeypatch, capsys):
    interactive(monkeypatch, True)
    assert run_cli(monkeypatch, "examples") == 0
    out = capsys.readouterr().out
    assert "anonymous usage stats" in out
    assert "pdt settings usage-stats off" in out
    values = settings.load()
    assert values["usage_stats"] is True
    assert values["install_id"]
    assert run_cli(monkeypatch, "examples") == 0
    assert "anonymous usage stats" not in capsys.readouterr().out


def test_no_notice_and_no_file_when_not_interactive(sent, monkeypatch, capsys):
    interactive(monkeypatch, False)
    assert run_cli(monkeypatch, "examples") == 0
    assert "anonymous usage stats" not in capsys.readouterr().out
    assert not settings.path().exists()
    assert sent == []


def test_no_notice_for_the_settings_command(sent, monkeypatch, capsys):
    interactive(monkeypatch, True)
    assert run_cli(monkeypatch, "settings") == 0
    assert "anonymous usage stats" not in capsys.readouterr().out
    assert not settings.path().exists()


def test_no_event_without_the_file(sent):
    usage.record(EXAMPLES, 0, 1.0)
    assert sent == []


def test_no_event_with_usage_off(sent):
    settings.save({"usage_stats": False})
    usage.record(EXAMPLES, 0, 1.0)
    assert sent == []


def test_no_event_with_an_empty_key(sent, monkeypatch):
    settings.save({"usage_stats": True})
    monkeypatch.setattr(usage, "USAGE_KEY", "")
    usage.record(EXAMPLES, 0, 1.0)
    assert sent == []


def test_no_event_under_do_not_track(sent, monkeypatch):
    settings.save({"usage_stats": True})
    monkeypatch.setenv("DO_NOT_TRACK", "1")
    usage.record(EXAMPLES, 0, 1.0)
    assert sent == []


def test_event_holds_no_app_name_or_argument(sent, project, monkeypatch):
    add_app(project, "payroll-export")
    settings.save({"usage_stats": True})
    monkeypatch.setattr(deploy, "runs", lambda name, options: 3)
    assert run_cli(monkeypatch, "runs", "payroll-export", "--since", "2026-09-20") == 3
    [event] = sent
    assert event["event"] == "pdt command"
    assert event["distinct_id"] == settings.load()["install_id"]
    properties = event["properties"]
    assert properties["command"] == "runs"
    assert properties["exit_code"] == 3
    assert properties["provider"] == "azure"
    assert properties["$process_person_profile"] is False
    body = json.dumps(event)
    assert "payroll-export" not in body
    assert "2026-09-20" not in body
    assert str(project) not in body


def test_config_error_is_recorded_as_exit_code_1(sent, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("PDT_PROJECT", raising=False)
    settings.save({"usage_stats": True})
    assert run_cli(monkeypatch, "list") == 1
    assert sent[0]["properties"]["exit_code"] == 1
    assert sent[0]["properties"]["provider"] is None


def test_a_failing_send_does_not_change_the_exit_code(sent, monkeypatch, capsys):
    settings.save({"usage_stats": True})

    def broken_urlopen(request, timeout):
        raise OSError("network is down")

    monkeypatch.setattr("urllib.request.urlopen", broken_urlopen)
    assert run_cli(monkeypatch, "examples") == 0
    assert "network is down" not in capsys.readouterr().out
