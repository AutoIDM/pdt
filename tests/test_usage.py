import json
import shlex
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest

from pdt import cli, config, console, deploy_common, lifecycle, scaffold, usage, watch

UUID = "11111111-1111-4111-8111-111111111111"
PROJECT = "22222222-2222-4222-8222-222222222222"


@pytest.fixture
def usage_on(tmp_path, monkeypatch):
    monkeypatch.delenv(usage.OFF_ENV)
    for name in ("DO_NOT_TRACK", usage.CLIENT_ENV, usage.PROJECT_ENV, usage.COMMAND_ENV):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(watch, "can_offer", lambda: False)
    return tmp_path / "data" / "pdt" / "usage"


@pytest.fixture
def posts(monkeypatch):
    sent = []
    monkeypatch.setattr(usage, "_post", sent.append)
    monkeypatch.setattr(usage, "_sender", None)
    monkeypatch.setattr(usage, "_sending", False)
    monkeypatch.setattr(usage, "_queued", [])
    monkeypatch.setattr(usage, "_changed", threading.Condition())
    return sent


def event(name="cli:command", **params):
    return lifecycle.Event(name, datetime(2026, 11, 2, tzinfo=timezone.utc), params)


@pytest.mark.parametrize("typed, expected", [
    ("pdt storage report ls exports --recursive", "pdt storage report ls exports --recursive"),
    ("pdt storage report get exports/a.csv ~/a.csv", "pdt storage report get exports/a.csv ~/a.csv"),
    ('pdt storage report query "select * from t"', "pdt storage report query <excluded>"),
    ('pdt storage query "select * from t"', "pdt storage query <excluded>"),
    ("pdt storage report query select * from t", "pdt storage report query <excluded>"),
    ('pdt storag report query "select 1"', "pdt storag report query <excluded>"),
    ("pdt register --code KQ7M-2RTX", "pdt register --code <excluded>"),
    ("pdt registr --code=KQ7M-2RTX", "pdt registr --code=<excluded>"),
    ("pdt az keyvault secret set --value hunter2", "pdt az keyvault secret set --value <excluded>"),
    ("pdt az storage list --connection-string a=b", "pdt az storage list --connection-string <excluded>"),
    ("pdt az storage list --sas-token=abc", "pdt az storage list --sas-token=<excluded>"),
    ("pdt aws login --pass hunter2", "pdt aws login --pass <excluded>"),
    ("pdt secrets report set API_KEY hunter2", "pdt secrets report set API_KEY <excluded>"),
    ("pdt secrets set API_KEY hunter2", "pdt secrets set API_KEY <excluded>"),
    ("pdt secrets report API_KEY=hunter2", "pdt secrets report <excluded>"),
    ("pdt secret report set API_KEY hunter2", "pdt secret report set API_KEY <excluded>"),
    ("pdt aws s3 ls --profile dev", "pdt aws s3 ls --profile dev"),
    ("pdt az login -u a@example.com -p pw", "pdt az login -u <excluded> -p <excluded>"),
    ("pdt az login --username=a@example.com -ppw", "pdt az login --username=<excluded> -p<excluded>"),
    ("pdt deploy /Users/ann/x", "pdt deploy ~/x"),
    ("pdt deploy /Users/annie/x", "pdt deploy /Users/annie/x"),
    ("pdt lgos KQ7M-2RTX", "pdt lgos <excluded>"),
    ("pdt lgos --name=KQ7M-2RTX", "pdt lgos --name=<excluded>"),
    ("pdt run a.b.c eyJhbGci.eyJzdWIi.sig", "pdt run a.b.c <excluded>"),
    ("pdt x AKIAABCDEFGHIJKLMNOP", "pdt x <excluded>"),
    ("pdt x postgres://u:pw@host/db", "pdt x <excluded>"),
    ("pdt x https://example.com/a:b", "pdt x https://example.com/a:b"),
    ('pdt new "my report"', "pdt new 'my report'"),
    ("pdt", "pdt"),
])
def test_command_line(typed, expected):
    assert usage.command_line(shlex.split(typed), home="/Users/ann") == expected


def test_command_line_has_a_maximum_length():
    assert len(usage.command_line(["pdt", "x" * 5000], home="")) == usage.MAX_COMMAND_LINE


def test_no_question_or_send_when_any_opt_out_is_set(usage_on, posts, monkeypatch):
    monkeypatch.setattr(watch, "can_offer", lambda: True)
    monkeypatch.setattr(console, "confirm", lambda *a, **k: pytest.fail("asked"))
    for name, value in (("DO_NOT_TRACK", "1"), (usage.OFF_ENV, "no"), (usage.OFF_ENV, "FALSE")):
        monkeypatch.setenv(name, value)
        monkeypatch.setattr(usage, "_resolved", False)
        usage.start(["pdt", "validate"])
        usage.accept(event())
        usage.wait()
        monkeypatch.delenv(name)
    assert posts == []
    assert usage._sender is None
    assert not usage_on.exists()


def test_project_setting_turns_it_off(usage_on, posts, tmp_path):
    (tmp_path / config.PROJECT_FILE).write_text(f"{config.USAGE_STATS_KEY}: false\n")
    usage.accept(event())
    assert usage._sender is None
    assert not usage_on.exists()


@pytest.mark.parametrize("value", ["0", ""])
def test_do_not_track_zero_or_empty_is_not_an_opt_out(usage_on, monkeypatch, value):
    monkeypatch.setenv("DO_NOT_TRACK", value)
    assert not usage.off()


def test_a_person_is_asked_once_and_enter_means_yes(usage_on, monkeypatch):
    asked = []
    monkeypatch.setattr(watch, "can_offer", lambda: True)
    monkeypatch.setattr(console, "confirm", lambda question, default: asked.append(
        (question, default)) or default)
    usage.start(["pdt", "validate"])
    monkeypatch.setattr(usage, "_resolved", False)
    usage.start(["pdt", "validate"])
    assert asked == [("Send anonymous usage statistics to AutoIDM?", True)]
    record = json.loads((usage_on / "client.json").read_text())
    assert record["asked"] is True
    assert str(uuid.UUID(record["client_uuid"])) == record["client_uuid"]
    assert usage.identity().client_uuid == record["client_uuid"]


def test_no_records_the_answer_with_no_uuid(usage_on, posts, monkeypatch):
    monkeypatch.setattr(watch, "can_offer", lambda: True)
    monkeypatch.setattr(console, "confirm", lambda question, default: False)
    usage.start(["pdt", "validate"])
    assert json.loads((usage_on / "client.json").read_text()) == {"client_uuid": None,
                                                                  "asked": True}
    usage.accept(event())
    assert usage._sender is None


@pytest.mark.parametrize("flag", ["--help", "-h", "--version", "--json", "--js", "--yes", "-y",
                                  "--no-usage-stats"])
def test_flags_skip_the_question_and_a_person_sends_nothing_yet(usage_on, monkeypatch, flag):
    monkeypatch.setattr(watch, "can_offer", lambda: True)
    monkeypatch.setattr(console, "confirm", lambda *a, **k: pytest.fail("asked"))
    usage.start(["pdt", "run", flag])
    assert usage.identity() is None
    assert not usage_on.exists()


def test_with_no_one_to_ask_it_sends_and_a_person_is_still_asked_later(usage_on, monkeypatch):
    usage.start(["pdt", "validate"])
    first = usage.identity().client_uuid
    assert json.loads((usage_on / "client.json").read_text()) == {"client_uuid": first,
                                                                  "asked": False}
    monkeypatch.setattr(usage, "_resolved", False)
    monkeypatch.setattr(watch, "can_offer", lambda: True)
    monkeypatch.setattr(console, "confirm", lambda question, default: True)
    usage.start(["pdt", "validate"])
    assert usage.identity().client_uuid == first
    assert json.loads((usage_on / "client.json").read_text())["asked"] is True


def test_rows_hold_only_the_allowed_fields(usage_on, tmp_path, posts):
    (tmp_path / config.PROJECT_FILE).write_text("apps: []\n")
    usage.accept(event("job:run-failed", project="secret-project", app="report", provider="aws",
                       place="aws:123456789012:us-east-2", run_id="r", state="runs/*-r/",
                       schedule="0 0 * * *", timezone="UTC", deployed=True, trigger="manual",
                       exit_code=1, reason="interrupted", signal="SIGINT"))
    usage.wait()
    row = posts[0][0]
    assert row["params"] == {"app": "report", "provider": "aws", "deployed": True,
                             "trigger": "manual", "exit_code": 1, "reason": "interrupted",
                             "signal": "SIGINT"}
    assert set(row) == {"name", "timestamp", "client_uuid", "project_uuid", "command_uuid",
                        "pdt_version", "python_version", "os", "architecture", "ci", "params"}
    assert row["timestamp"] == "2026-11-02T00:00:00.000Z"
    assert row["ci"] is False
    uuid.UUID(row["project_uuid"])
    assert str(tmp_path) not in json.dumps(row)


def test_project_uuid_is_null_with_no_project_and_stable_per_path(usage_on, tmp_path, monkeypatch):
    assert usage.identity().project_uuid is None
    monkeypatch.setattr(usage, "_resolved", False)
    (tmp_path / config.PROJECT_FILE).write_text("apps: []\n")
    first = usage.identity().project_uuid
    monkeypatch.setattr(usage, "_resolved", False)
    assert usage.identity().project_uuid == first
    other = tmp_path / "other"
    other.mkdir()
    (other / config.PROJECT_FILE).write_text("apps: []\n")
    monkeypatch.chdir(other)
    monkeypatch.setattr(usage, "_resolved", False)
    assert usage.identity().project_uuid != first


def test_a_child_process_keeps_the_command_uuid(usage_on, monkeypatch):
    monkeypatch.setenv(usage.COMMAND_ENV, UUID)
    assert usage.identity().command_uuid == UUID


def test_app_events_are_not_sent(usage_on, posts):
    lifecycle.accept_usage(event("my:custom"))
    assert usage._sender is None
    lifecycle.accept_usage(event(lifecycle.JOB_PAUSED))
    assert usage._sender is not None


def test_events_queued_during_a_send_go_as_one_request(usage_on, monkeypatch, posts):
    release = threading.Event()
    sent = []

    def slow(rows):
        sent.append([row["name"] for row in rows])
        release.wait(5)

    monkeypatch.setattr(usage, "_post", slow)
    usage.accept(event("job:paused"))
    while not sent:
        pass
    usage.accept(event("job:unpaused"))
    usage.accept(event("cli:command"))
    release.set()
    usage.wait()
    assert sent == [["job:paused"], ["job:unpaused", "cli:command"]]


def test_a_failed_send_is_silent(usage_on, monkeypatch, posts, capfd):
    def fail(rows):
        raise OSError("no network")

    monkeypatch.setattr(usage, "_post", fail)
    usage.accept(event())
    usage.wait()
    assert capfd.readouterr() == ("", "")


def test_wait_gives_up_once_for_the_whole_process(usage_on, monkeypatch, posts):
    release = threading.Event()
    monkeypatch.setattr(usage, "_post", lambda rows: release.wait(5))
    usage.accept(event())
    usage.wait(0.01)
    assert usage._gave_up
    usage.accept(event())
    assert usage._queued == []
    release.set()


def test_a_job_uses_its_environment_and_writes_nothing(usage_on, monkeypatch):
    usage.as_job()
    monkeypatch.setenv(usage.CLIENT_ENV, UUID)
    monkeypatch.setenv(usage.PROJECT_ENV, PROJECT)
    monkeypatch.setenv(usage.OFF_ENV, "true")
    found = usage.identity()
    assert (found.client_uuid, found.project_uuid) == (UUID, PROJECT)
    assert not usage_on.exists()


@pytest.mark.parametrize("absent", [usage.CLIENT_ENV, usage.PROJECT_ENV, usage.OFF_ENV])
def test_a_job_with_any_value_absent_sends_nothing(usage_on, monkeypatch, absent):
    usage.as_job()
    monkeypatch.setenv(usage.CLIENT_ENV, UUID)
    monkeypatch.setenv(usage.PROJECT_ENV, PROJECT)
    monkeypatch.setenv(usage.OFF_ENV, "true")
    monkeypatch.delenv(absent)
    assert usage.identity() is None
    assert not usage_on.exists()


def test_job_env_passes_the_identity_or_the_opt_out(usage_on, tmp_path, monkeypatch):
    (tmp_path / config.PROJECT_FILE).write_text("apps: []\n")
    env = usage.job_env()
    assert set(env) == {usage.OFF_ENV, usage.CLIENT_ENV, usage.PROJECT_ENV, "PDT_CLI_VERSION"}
    assert env[usage.OFF_ENV] == "true"
    monkeypatch.setattr(usage, "_resolved", False)
    monkeypatch.setenv("DO_NOT_TRACK", "1")
    assert usage.job_env() == {usage.OFF_ENV: "false"}


def test_usage_is_in_the_job_image():
    assert "usage.py" in deploy_common.LIFECYCLE_MODULES


def test_usage_imports_no_third_party_package():
    source = (Path(usage.__file__)).read_text()
    imported = {line.split()[1].split(".")[0] for line in source.splitlines()
                if line.startswith(("import ", "from "))}
    assert imported == {"__future__", "difflib", "hashlib", "json", "os", "re", "shlex", "sys",
                        "threading", "uuid", "dataclasses", "pathlib", "pdt"}


def test_exception_fields_name_the_pdt_file_and_no_message():
    try:
        raise ValueError("customer data")
    except ValueError as error:
        found = usage.exception_fields(error)
    assert found == {"type": "ValueError", "file": None, "line": None}


def test_exception_fields_of_a_pdt_frame(tmp_path):
    bad = tmp_path / "bad.yml"
    bad.write_text("a: [\n")
    with pytest.raises(config.ConfigError) as caught:
        config.load_yaml(bad)
    found = usage.exception_fields(caught.value)
    assert found["type"] == "ConfigError"
    assert found["file"] == "pdt/config.py"
    assert isinstance(found["line"], int)
    assert "bad.yml" not in json.dumps(found)


@pytest.mark.parametrize("argv, code", [
    (["pdt", "--version"], 0),
    (["pdt", "--help"], 0),
    (["pdt"], 0),
    (["pdt", "lgos"], 2),
    (["pdt", "validate", "--nope"], 2),
    (["pdt", "validate"], 1),
])
def test_each_command_ends_with_one_cli_event(usage_on, posts, monkeypatch, argv, code, capsys):
    monkeypatch.setattr("sys.argv", argv)
    try:
        result = cli.main()
    except SystemExit as stopped:
        result = stopped.code or 0
    assert result == code
    rows = [row for batch in posts for row in batch]
    assert [row["name"] for row in rows] == ["cli:command"]
    assert rows[0]["params"]["exit_code"] == code
    assert rows[0]["params"]["command_line"] == " ".join(argv)
    assert rows[0]["params"]["duration_seconds"] >= 0
    if argv[1:] == ["validate"]:
        assert rows[0]["params"]["exception"]["type"] == "ConfigError"


def test_a_failure_to_emit_never_changes_the_result(usage_on, monkeypatch, capsys):
    def fail(*args, **kwargs):
        raise OSError("boom")

    monkeypatch.setattr(lifecycle, "emit", fail)
    monkeypatch.setattr("sys.argv", ["pdt"])
    assert cli.main() == 0


def test_init_no_usage_stats_writes_the_key_and_sends_nothing(usage_on, posts, tmp_path, monkeypatch):
    monkeypatch.setattr("sys.argv", ["pdt", "init", "--yes", "--no-usage-stats"])
    assert cli.main() == 0
    assert config.load_yaml(tmp_path / config.PROJECT_FILE)[config.USAGE_STATS_KEY] is False
    assert usage._sender is None
    assert not usage_on.exists()


def test_init_no_usage_stats_changes_an_existing_project(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / config.PROJECT_FILE).write_text(f"# keep\nappsx: 1\n{config.USAGE_STATS_KEY}: true\n")
    assert scaffold.init(None, assume_yes=True, usage_stats=False) == 0
    text = (tmp_path / config.PROJECT_FILE).read_text()
    assert "# keep" in text
    assert config.load_yaml(tmp_path / config.PROJECT_FILE)[config.USAGE_STATS_KEY] is False
