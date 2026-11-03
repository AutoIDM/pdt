import json
import os
import re
import shlex
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from pdt import cli, config, console, deploy_common, lifecycle, scaffold, usage, watch

UUID = "11111111-1111-4111-8111-111111111111"
PROJECT = "22222222-2222-4222-8222-222222222222"
UPPERCASE = "ABCDEFAB-1111-4111-8111-111111111111"


@pytest.fixture
def usage_on(tmp_path, monkeypatch):
    monkeypatch.delenv(usage.OFF_ENV)
    for name in ("DO_NOT_TRACK", usage.CLIENT_ENV, usage.PROJECT_ENV, usage.COMMAND_ENV):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "data"))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    return tmp_path / "data" / "pdt" / "usage"


@pytest.fixture
def at_terminal(monkeypatch):
    for stream in ("stdin", "stdout", "stderr"):
        monkeypatch.setattr(f"sys.{stream}.isatty", lambda: True)


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


@pytest.fixture
def apps(monkeypatch):
    monkeypatch.setattr(config, "app_folders", lambda: ["hello-world", "report"])


@pytest.mark.parametrize("typed, expected", [
    ("pdt deploy hello-world --yes", "pdt deploy hello-world --yes"),
    ("pdt deplyo hello-world", "pdt deplyo hello-world"),
    ("pdt az containerapp secret set -n app -g rg --secrets db=Hunter2pw api=Zx9QpLm",
     "pdt az containerapp secret set -n <excluded> -g <excluded> --secrets <excluded> <excluded>"),
    ("pdt gcloud run jobs update app --set-env-vars DB_PASSWORD=Hunter2pw",
     "pdt gcloud run jobs update app --set-env-vars <excluded>"),
    ("pdt az storage blob upload --blob-url https://acct.blob.core.windows.net/c/f?sv=2024&sig=AbC123xYz",
     "pdt az storage blob upload --blob-url <excluded>"),
    ("pdt --project /Users/jane/acme secrets set payroll SMTP_PASSWORD Hunter2pw",
     "pdt <excluded> <excluded> secrets set <excluded> <excluded> <excluded>"),
    ("pdt aws s3 cp s3://customer-bucket/export.csv .", "pdt aws s3 cp <excluded> <excluded>"),
    ("pdt register --code ABCD-EFGH", "pdt register --code <excluded>"),
    ("pdt register --code=ABCD-EFGH", "pdt register --code=<excluded>"),
    ("pdt register --co ABCD-EFGH", "pdt register --co <excluded>"),
    ("pdt registr --code --code hunter2", "pdt registr --code --code <excluded>"),
    ("pdt storage report query 'select * from t'", "pdt storage report query <excluded>"),
    ("pdt storage report ls exports --recursive", "pdt storage report ls <excluded> --recursive"),
    ("pdt secrets report set API_KEY hunter2", "pdt secrets report set <excluded> <excluded>"),
    ("pdt secrets report diff -y", "pdt secrets report diff -y"),
    ("pdt runs report --count 5 --json", "pdt runs report --count 5 --json"),
    ("pdt runs report --count=5 --since=12h", "pdt runs report --count=5 --since=<excluded>"),
    ("pdt init --yes", "pdt init --yes"),
    ("pdt completion zsh --script", "pdt completion zsh --script"),
    ("pdt alerts setup", "pdt alerts setup"),
    ("pdt alerts --wat=hunter2", "pdt alerts <excluded>"),
    ("pdt validate --nope", "pdt validate <excluded>"),
    ("pdt lgos hunter2", "pdt lgos <excluded>"),
    ("pdt hunter2 deploy", "pdt <excluded> deploy"),
    ("pdt hunter2", "pdt <excluded>"),
    ("pdt az login -u a@example.com -phunter2", "pdt az login -u <excluded> <excluded>"),
    ("pdt az login --username=a@example.com", "pdt az login --username=<excluded>"),
    ("pdt az keyvault secret set --name n --value hunter2 hunter3",
     "pdt az keyvault secret set --name <excluded> --value <excluded> <excluded>"),
    ("pdt aws s3 ls --profile dev", "pdt aws s3 ls --profile <excluded>"),
    ("pdt aws S3cr3t ls", "pdt aws <excluded> ls"),
    ("pdt aws --Hunter2 x", "pdt aws <excluded> <excluded>"),
    ("pdt gclod customer-secret", "pdt gclod <excluded>"),
    ("pdt --unknown aws customer-secret", "pdt <excluded> aws <excluded>"),
    ("pdt", "pdt"),
])
def test_command_line(apps, typed, expected):
    assert usage.command_line(shlex.split(typed), cli.build_parser(), home="/Users/ann") == expected


def test_command_line_keeps_a_region_a_provider_and_a_schedule_word(apps):
    assert (usage.command_line(["pdt", "deploy", "eastus2", "aws", "daily", "1.5"],
                               cli.build_parser(), home="")
            == "pdt deploy eastus2 aws daily 1.5")


@pytest.mark.parametrize("word, expected", [
    (r"c:\users\ann\project", r"~\project"),
    (r"C:\Users\Ann\project", r"~\project"),
    ("C:/Users/Ann/project", "~/project"),
    (r"c:/USERS\ann", "~"),
    (r"--folder=c:\users\ann\project", r"--folder=~\project"),
    (r"c:\users\annie\project", r"c:\users\annie\project"),
    (r"home=c:\users\ann,team=it", "home=~,team=it"),
])
def test_home_to_tilde_finds_a_windows_home_in_any_case_and_separator(word, expected):
    assert usage.home_to_tilde([word], home="C:\\Users\\Ann\\") == [expected]


@pytest.mark.parametrize("word, expected", [
    ("/Users/ann/x", "~/x"),
    ("/users/ann/x", "/users/ann/x"),
    ("/Users/annie/x", "/Users/annie/x"),
    ("home=/Users/ann,team=it", "home=~,team=it"),
    ('{"path":"/Users/ann"}', '{"path":"~"}'),
    ("/Users/ann;x", "~;x"),
    ("a=/Users/ann=b", "a=~=b"),
    ("'/Users/ann'", "'~'"),
])
def test_home_to_tilde_ends_the_home_at_a_separator_or_a_quote(word, expected):
    assert usage.home_to_tilde([word], home="/Users/ann") == [expected]


def test_command_line_has_a_maximum_length(apps):
    argv = ["pdt", "deploy", *["--yes"] * 500]
    assert len(usage.command_line(argv, cli.build_parser(), home="")) == usage.MAX_COMMAND_LINE


def test_no_question_or_send_when_any_opt_out_is_set(usage_on, at_terminal, posts, monkeypatch):
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


def test_a_person_is_asked_once_and_enter_means_yes(usage_on, at_terminal, monkeypatch):
    asked = []
    monkeypatch.setattr(console, "confirm", lambda question, default, stderr=False: asked.append(
        (question, default)) or default)
    usage.start(["pdt", "validate"])
    monkeypatch.setattr(usage, "_resolved", False)
    usage.start(["pdt", "validate"])
    assert asked == [("Send anonymous usage statistics to AutoIDM?", True)]
    record = json.loads((usage_on / "client.json").read_text())
    assert record["asked"] is True
    assert str(uuid.UUID(record["client_uuid"])) == record["client_uuid"]
    assert usage.identity().client_uuid == record["client_uuid"]


def test_no_records_the_answer_with_no_uuid(usage_on, at_terminal, posts, monkeypatch):
    monkeypatch.setattr(console, "confirm", lambda question, default, stderr=False: False)
    usage.start(["pdt", "validate"])
    assert json.loads((usage_on / "client.json").read_text()) == {"client_uuid": None,
                                                                  "asked": True}
    usage.accept(event())
    assert usage._sender is None


@pytest.mark.parametrize("flag", ["--yes", "--ye", "-y"])
def test_yes_answers_the_question_with_yes(usage_on, at_terminal, monkeypatch, flag):
    monkeypatch.setattr(console, "confirm", lambda *a, **k: pytest.fail("asked"))
    usage.start(["pdt", "deploy", "my-app", "--json", flag])
    record = json.loads((usage_on / "client.json").read_text())
    assert record["asked"] is True
    assert usage.identity().client_uuid == record["client_uuid"]


@pytest.mark.parametrize("name, value", [("DO_NOT_TRACK", "1"), (usage.OFF_ENV, "off"),
                                         ("project", None), ("flag", None)])
def test_each_opt_out_is_stronger_than_yes(usage_on, at_terminal, tmp_path, monkeypatch,
                                           name, value):
    argv = ["pdt", "init", "--yes"]
    if name == "project":
        (tmp_path / config.PROJECT_FILE).write_text(f"{config.USAGE_STATS_KEY}: false\n")
    elif name == "flag":
        argv.append(usage.OPT_OUT_FLAG)
    else:
        monkeypatch.setenv(name, value)
    usage.start(argv)
    assert usage.identity() is None
    assert not usage_on.exists()


def test_yes_of_a_cloud_cli_is_not_an_answer(usage_on, at_terminal, monkeypatch):
    monkeypatch.setattr(console, "confirm", lambda *a, **k: False)
    usage.start(["pdt", "az", "group", "delete", "--yes"])
    assert json.loads((usage_on / "client.json").read_text()) == {"client_uuid": None,
                                                                  "asked": True}


@pytest.mark.parametrize("flag", ["--help", "-h", "--version", "--json", "--js",
                                  "--no-usage-stats", "--plan", "--pl", "--names", "--nam",
                                  "--script", "--scr"])
def test_flags_skip_the_question_and_a_person_sends_nothing_yet(
        usage_on, at_terminal, monkeypatch, flag):
    argv = ["pdt", "run", flag]
    monkeypatch.setattr("sys.argv", argv)
    monkeypatch.setattr(console, "confirm", lambda *a, **k: pytest.fail("asked"))
    usage.start(argv)
    assert usage.identity() is None
    assert not usage_on.exists()


def test_a_quiet_command_at_a_terminal_sends_nothing_through_the_real_cli(
        usage_on, at_terminal, posts, tmp_path, monkeypatch):
    (tmp_path / config.PROJECT_FILE).write_text("apps: []\n")
    monkeypatch.setattr(console, "confirm", lambda *a, **k: pytest.fail("asked"))
    for argv in (["pdt", "list", "--json"], ["pdt", "list", "--js"], ["pdt", "list", "--names"]):
        monkeypatch.setattr("sys.argv", argv)
        monkeypatch.setattr(usage, "_resolved", False)
        assert cli.main() == 0
    usage.wait()
    assert posts == []
    assert not usage_on.exists()


def test_with_no_one_to_ask_a_refusal_saved_after_the_read_stays(usage_on, monkeypatch):
    refusal = json.dumps({"client_uuid": None, "asked": True})
    real_read = usage._read_client
    reads = []

    def read():
        found = real_read()
        if not reads:
            usage_on.mkdir(parents=True)
            (usage_on / "client.json").write_text(refusal)
        reads.append(found)
        return found

    monkeypatch.setattr(usage, "_read_client", read)
    assert usage.identity() is None
    assert (usage_on / "client.json").read_text() == refusal
    assert (sorted(path.name for path in usage_on.iterdir())
            == ["client.json", "client.json.lock"])


def test_the_repair_of_a_bad_file_waits_for_a_refusal_being_saved(usage_on):
    usage_on.mkdir(parents=True)
    (usage_on / "client.json").write_text("{")
    refusal = json.dumps({"client_uuid": None, "asked": True})
    found = []
    with config.locked(usage_on / "client.json"):
        thread = threading.Thread(target=lambda: found.append(usage.identity()))
        thread.start()
        thread.join(0.5)
        assert thread.is_alive()
        (usage_on / "client.json").write_text(refusal)
    thread.join(5)
    assert found == [None]
    assert (usage_on / "client.json").read_text() == refusal


def test_two_first_commands_in_a_project_send_one_project_uuid(usage_on, monkeypatch, tmp_path):
    (tmp_path / config.PROJECT_FILE).write_text("apps: []\n")
    real_create = usage._create

    def create(path, text):
        if path.parent.name == "projects":
            real_create(path, PROJECT)
        return real_create(path, text)

    monkeypatch.setattr(usage, "_create", create)
    assert usage.identity().project_uuid == PROJECT


def test_with_no_one_to_ask_a_record_saved_after_the_read_is_used(usage_on, monkeypatch):
    real_read = usage._read_client
    reads = []

    def read():
        found = real_read()
        if not reads:
            usage_on.mkdir(parents=True)
            (usage_on / "client.json").write_text(
                json.dumps({"client_uuid": UUID, "asked": False}))
        reads.append(found)
        return found

    monkeypatch.setattr(usage, "_read_client", read)
    assert usage.identity().client_uuid == UUID


def test_with_no_one_to_ask_a_bad_file_gets_a_new_record(usage_on):
    usage_on.mkdir(parents=True)
    (usage_on / "client.json").write_text("not json")
    found = usage.identity().client_uuid
    assert json.loads((usage_on / "client.json").read_text()) == {"client_uuid": found,
                                                                  "asked": False}


@pytest.fixture
def stdout_piped(at_terminal, monkeypatch):
    monkeypatch.setattr("sys.stdout.isatty", lambda: False)


@pytest.mark.parametrize("argv", [["pdt", "list", "--json"], ["pdt", "list", "--js"],
                                  ["pdt", "list", "--names"]])
def test_a_person_with_piped_stdout_in_a_quiet_command_sends_nothing(
        usage_on, stdout_piped, monkeypatch, argv):
    monkeypatch.setattr("sys.argv", argv)
    monkeypatch.setattr(console, "confirm", lambda *a, **k: pytest.fail("asked"))
    usage.start(argv)
    assert usage.identity() is None
    assert not usage_on.exists()


def test_a_person_with_piped_stdout_is_asked_on_stderr(usage_on, monkeypatch, capsys):
    for stream, value in (("stdin", True), ("stdout", False), ("stderr", True)):
        monkeypatch.setattr(f"sys.{stream}.isatty", lambda value=value: value)
    monkeypatch.setattr("builtins.input", lambda *args: "n")
    usage.start(["pdt", "list"])
    out, err = capsys.readouterr()
    assert usage.QUESTION in err
    assert out == ""
    assert json.loads((usage_on / "client.json").read_text()) == {"client_uuid": None,
                                                                  "asked": True}


def test_a_wrong_answer_repeats_on_stderr(usage_on, monkeypatch, capsys):
    for stream, value in (("stdin", True), ("stdout", False), ("stderr", True)):
        monkeypatch.setattr(f"sys.{stream}.isatty", lambda value=value: value)
    answers = iter(("x", "y"))
    monkeypatch.setattr("builtins.input", lambda *args: next(answers))
    usage.start(["pdt", "list"])
    out, err = capsys.readouterr()
    assert "Please answer y or n." in err
    assert out == ""


@pytest.mark.parametrize("name", ["CI", "CLOUD_RUN_JOB", *watch.AGENT_ENVS])
def test_ci_and_an_agent_are_not_a_person(usage_on, at_terminal, monkeypatch, name):
    monkeypatch.setenv(name, "1")
    assert not watch.person_at_terminal()


def test_a_terminal_is_stdin_and_stderr(usage_on, at_terminal, monkeypatch):
    assert watch.person_at_terminal()
    monkeypatch.setattr("sys.stdout.isatty", lambda: False)
    assert watch.person_at_terminal()
    monkeypatch.setattr("sys.stderr.isatty", lambda: False)
    assert not watch.person_at_terminal()
    monkeypatch.setattr("sys.stderr.isatty", lambda: True)
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    assert not watch.person_at_terminal()


def test_the_help_line_still_needs_stdout_at_a_terminal(usage_on, at_terminal, monkeypatch):
    monkeypatch.setattr("sys.argv", ["pdt", "validate"])
    assert watch.can_offer()
    monkeypatch.setattr("sys.stdout.isatty", lambda: False)
    assert not watch.can_offer()


def test_each_generated_text_field_has_the_maximum_length(usage_on, monkeypatch):
    who = usage.Identity(UUID, None, PROJECT)
    long_word = "x" * 200
    monkeypatch.setattr("pdt.__version__", long_word)
    monkeypatch.setattr(usage.os, "uname", lambda: SimpleNamespace(machine=long_word),
                        raising=False)
    with monkeypatch.context() as patched:
        patched.setattr(sys, "platform", long_word)
        patched.setattr(sys, "version_info", (3, 12, long_word))
        row = usage._row(event(), who)
    assert {field: len(row[field]) for field in ("pdt_version", "python_version", "os",
                                                 "architecture")} == dict.fromkeys(
        ("pdt_version", "python_version", "os", "architecture"), usage.MAX_TEXT)


def test_with_no_one_to_ask_it_sends_and_a_person_is_still_asked_later(usage_on, monkeypatch):
    usage.start(["pdt", "validate"])
    first = usage.identity().client_uuid
    assert json.loads((usage_on / "client.json").read_text()) == {"client_uuid": first,
                                                                  "asked": False}
    monkeypatch.setattr(usage, "_resolved", False)
    for stream in ("stdin", "stdout", "stderr"):
        monkeypatch.setattr(f"sys.{stream}.isatty", lambda: True)
    monkeypatch.setattr(console, "confirm", lambda question, default, stderr=False: True)
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


def test_a_value_outside_the_known_values_is_dropped(usage_on, posts):
    usage.accept(event("job:run-failed", reason="hunter2", action="hunter2", signal="hunter2",
                       trigger="hunter2", provider="hunter2", exit_code="hunter2",
                       count="hunter2", deployed="hunter2", duration_seconds="hunter2",
                       app=7, command_line=7, exception="hunter2"))
    usage.accept(event("job:run-failed", exit_code=True, count=2**63,
                       duration_seconds=float("nan"), app="x" * 256, command_line="x" * 1025,
                       exception={"type": "E", "file": "f", "line": 1, "message": "hunter2"}))
    usage.wait()
    assert [row["params"] for batch in posts for row in batch] == [{}, {}]


def test_an_event_sends_only_the_command_line_and_exception_this_process_built(usage_on, posts):
    exception = {"type": "ValueError", "file": "/Users/ann/customer.txt", "line": 1}
    usage.accept(event("job:run-failed", command_line="pdt register --code hunter2",
                       exception=exception))
    built = usage.command_line(["pdt", "register", "--code", "hunter2"], cli.build_parser())
    try:
        raise ValueError("hunter2")
    except ValueError as error:
        fields = usage.exception_fields(error)
    usage.accept(event(command_line=built, exception=fields))
    usage.wait()
    assert [row["params"] for batch in posts for row in batch] == [
        {}, {"command_line": "pdt register --code <excluded>", "exception": fields}]


def test_a_changed_exception_dict_is_not_sent(usage_on, posts):
    changed = usage.exception_fields(ValueError("hunter2"))
    changed["file"] = "/Users/ann/customer.txt"
    usage.accept(event(exception=changed))
    sent = usage.exception_fields(ValueError("hunter2"))
    usage.accept(event(exception=sent))
    sent["message"] = "hunter2"
    usage.wait()
    assert [row["params"] for batch in posts for row in batch] == [
        {}, {"exception": {"type": "ValueError", "file": None, "line": None}}]


def test_a_second_yes_keeps_the_client_uuid_another_command_saved(usage_on, at_terminal,
                                                                  monkeypatch):
    def answer_after_another_command_saved(question, default, stderr=False):
        usage_on.mkdir(parents=True)
        (usage_on / "client.json").write_text(json.dumps({"client_uuid": UUID, "asked": True}))
        return True

    monkeypatch.setattr(console, "confirm", answer_after_another_command_saved)
    usage.start(["pdt", "validate"])
    assert json.loads((usage_on / "client.json").read_text()) == {"client_uuid": UUID,
                                                                  "asked": True}


def test_the_known_values_match_the_source():
    assert usage.KNOWN_VALUES == {
        "provider": ("google-cloud", "azure", "aws", "windows"),
        "trigger": ("manual", "schedule"),
        "reason": ("declined", "interrupted", "unauthorized"),
        "signal": ("SIGINT", "SIGTERM"),
        "action": ("diff", "save", "get", "set"),
    }
    assert usage.KNOWN_VALUES["provider"] == config.PROVIDERS
    assert usage.KNOWN_VALUES["action"] == config.SECRET_ACTIONS
    source = "\n".join(path.read_text(encoding="utf-8")
                       for path in Path(usage.__file__).parent.glob("*.py"))
    reasons = set(re.findall(r"""\breason"?\s*[=:]\s*"([a-z-]+)"(?: if .* else "([a-z-]+)")?""",
                             source))
    assert {word for found in reasons for word in found if word} == set(
        usage.KNOWN_VALUES["reason"])


@pytest.mark.parametrize("record", [
    "[]",
    "{}",
    '{"asked": true}',
    f'{{"client_uuid": "{UUID}"}}',
    f'{{"client_uuid": "{UUID}", "asked": "false"}}',
    f'{{"client_uuid": "{UUID}", "asked": 1}}',
    '{"client_uuid": "not-a-uuid", "asked": true}',
    f'{{"client_uuid": "{UPPERCASE}", "asked": true}}',
    '{"client_uuid": 7, "asked": true}',
    '{"client_uuid": null, "asked": false}',
    "not json",
    pytest.param("[" * 200_000, id="deep"),
])
def test_a_bad_client_file_counts_as_no_file(usage_on, at_terminal, monkeypatch, record):
    usage_on.mkdir(parents=True)
    (usage_on / "client.json").write_text(record)
    asked = []
    monkeypatch.setattr(console, "confirm", lambda question, default, stderr=False: asked.append(question) or True)
    usage.start(["pdt", "validate"])
    assert len(asked) == 1
    kept = json.loads((usage_on / "client.json").read_text())
    assert kept["asked"] is True
    assert usage.identity().client_uuid == str(uuid.UUID(kept["client_uuid"]))
    assert usage.identity().client_uuid != UUID


@pytest.mark.parametrize("contents", [b"not-a-uuid\n", b"", b"\xff\xfe", UPPERCASE.encode()])
def test_a_bad_project_file_gets_a_new_uuid(usage_on, tmp_path, contents):
    (tmp_path / config.PROJECT_FILE).write_text("apps: []\n")
    first = usage.identity().project_uuid
    path = next((usage_on / "projects").iterdir())
    path.write_bytes(contents)
    usage._resolved = False
    second = usage.identity().project_uuid
    assert second != first
    assert second == str(uuid.UUID(second))
    assert path.read_text() == second


@pytest.mark.parametrize("bad", [usage.CLIENT_ENV, usage.PROJECT_ENV])
def test_a_job_with_a_bad_uuid_sends_nothing(usage_on, monkeypatch, bad):
    usage.as_job()
    monkeypatch.setenv(usage.CLIENT_ENV, UUID)
    monkeypatch.setenv(usage.PROJECT_ENV, PROJECT)
    monkeypatch.setenv(usage.OFF_ENV, "true")
    monkeypatch.setenv(bad, "not-a-uuid")
    assert usage.identity() is None


def test_a_bad_command_uuid_gets_a_new_uuid(usage_on, monkeypatch):
    monkeypatch.setenv(usage.COMMAND_ENV, "not-a-uuid")
    found = usage.identity().command_uuid
    assert found == str(uuid.UUID(found))
    assert usage.identity().command_uuid == os.environ[usage.COMMAND_ENV]


@pytest.mark.parametrize("name, value", [("DO_NOT_TRACK", "1"), (usage.OFF_ENV, "off")])
def test_an_opt_out_after_the_start_stops_the_next_event(usage_on, posts, monkeypatch, name, value):
    usage.start(["pdt", "run"])
    assert usage.identity() is not None
    monkeypatch.setenv(name, value)
    usage.accept(event())
    usage.wait()
    assert posts == []
    assert usage._sender is None


def test_each_event_reads_no_project_file_for_the_opt_out(usage_on, posts, monkeypatch):
    usage.start(["pdt", "run"])
    monkeypatch.setattr(config, "load_yaml", lambda path: pytest.fail("read the project file"))
    usage.accept(event())
    usage.wait()
    assert len(posts) == 1


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


def test_a_wait_with_no_unfinished_send_uses_no_allowance(usage_on, monkeypatch, posts):
    monkeypatch.setattr(usage, "EXIT_WAIT_SECONDS", 0.5)
    usage.wait()
    time.sleep(0.55)
    release = threading.Timer(0.05, lambda: None)
    release.start()
    monkeypatch.setattr(usage, "_post", lambda rows: release.join(5))
    usage.accept(event())
    usage.wait()
    assert not usage._gave_up


def test_the_allowance_adds_up_over_each_real_wait(usage_on, monkeypatch, posts):
    monkeypatch.setattr(usage, "EXIT_WAIT_SECONDS", 0.5)
    first = threading.Timer(0.3, lambda: None)
    first.start()
    never = threading.Event()
    monkeypatch.setattr(usage, "_post", lambda rows: first.join(5))
    usage.accept(event())
    usage.wait()
    monkeypatch.setattr(usage, "_post", lambda rows: never.wait(5))
    usage.accept(event())
    began = time.monotonic()
    usage.wait()
    waited = time.monotonic() - began
    never.set()
    assert usage._gave_up
    assert waited < 0.45


def test_a_wait_does_not_depend_on_identity_file_work(usage_on, monkeypatch, posts):
    entered = threading.Event()
    release = threading.Event()

    def slow():
        entered.set()
        release.wait(5)

    monkeypatch.setattr(usage, "_resolve", slow)
    resolving = threading.Thread(target=usage.identity, daemon=True)
    resolving.start()
    assert entered.wait(5)
    began = time.monotonic()
    usage.wait(0.01)
    waited = time.monotonic() - began
    release.set()
    resolving.join(5)
    assert waited < 2


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


def test_a_child_of_a_job_writes_no_identity_file(usage_on, posts, tmp_path):
    (tmp_path / config.PROJECT_FILE).write_text("apps: []\n")
    assert lifecycle.main(["--", sys.executable, "-c",
                           "from pdt import usage; usage.identity()"]) == 0
    assert not usage_on.exists()


def test_usage_is_in_the_job_image():
    assert "usage.py" in deploy_common.LIFECYCLE_MODULES


def test_usage_imports_no_third_party_package():
    source = (Path(usage.__file__)).read_text()
    imported = {line.split()[1].split(".")[0] for line in source.splitlines()
                if line.startswith(("import ", "from "))}
    assert imported == {"__future__", "argparse", "difflib", "hashlib", "json", "math", "os", "re",
                        "shlex", "sys", "threading", "time", "uuid", "dataclasses", "pathlib",
                        "pdt"}


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
    (["pdt", "validate", "--yes"], 2),
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


@pytest.mark.parametrize("off", [False, True])
def test_a_deep_client_file_does_not_change_a_command(usage_on, monkeypatch, capsys, off):
    if off:
        monkeypatch.setenv(usage.OFF_ENV, "false")
    usage_on.mkdir(parents=True)
    (usage_on / "client.json").write_text("[" * 200_000)
    monkeypatch.setattr("sys.argv", ["pdt", "--help"])
    with pytest.raises(SystemExit) as stopped:
        cli.main()
    assert stopped.value.code == 0
    assert "Traceback" not in capsys.readouterr().err


def test_a_thread_that_does_not_start_does_not_stop_a_run(usage_on, tmp_path, monkeypatch, capsys):
    def fail(*args, **kwargs):
        raise RuntimeError("can't start new thread")

    (tmp_path / config.PROJECT_FILE).write_text("apps: []\n")
    monkeypatch.setattr(usage, "_post", lambda rows: pytest.fail("sent"))
    monkeypatch.setattr(usage, "threading", SimpleNamespace(Thread=fail))
    monkeypatch.setattr(usage, "_sender", None)
    monkeypatch.setattr(usage, "_queued", [])
    began = time.monotonic()
    marker = tmp_path / "ran"
    code = lifecycle.run_job("report", "aws", [
        sys.executable, "-c", f"open({str(marker)!r}, 'w').close(); raise SystemExit(3)"], tmp_path)
    assert code == 3
    assert marker.exists()
    assert usage._queued == []
    assert time.monotonic() - began < usage.EXIT_WAIT_SECONDS


@pytest.mark.parametrize("call", [lambda: usage.start(["pdt", "run"]),
                                  lambda: usage.accept(event())])
def test_any_exception_stays_inside_start_and_accept(usage_on, monkeypatch, call):
    def fail():
        raise ZeroDivisionError

    monkeypatch.setattr(usage, "_resolve", fail)
    call()


def test_any_exception_in_job_env_means_off(usage_on, monkeypatch):
    def fail():
        raise ZeroDivisionError

    monkeypatch.setattr(usage, "_resolve", fail)
    assert usage.job_env() == {usage.OFF_ENV: "false"}


def test_a_waiter_that_does_not_start_keeps_the_result(usage_on, monkeypatch):
    real_start = threading.Thread.start
    tried = []

    def start(thread):
        if thread._target is usage.wait:
            tried.append(thread)
            raise RuntimeError("can't start new thread")
        real_start(thread)

    monkeypatch.setattr(threading.Thread, "start", start)
    monkeypatch.setattr("sys.argv", ["pdt", "--version"])
    with pytest.raises(SystemExit) as stopped:
        cli.main()
    assert stopped.value.code == 0
    assert tried != []


def test_a_waiter_that_does_not_start_still_waits_for_its_sink(monkeypatch):
    waited = []

    def fail(thread):
        raise RuntimeError("can't start new thread")

    monkeypatch.setattr(threading.Thread, "start", fail)
    monkeypatch.setattr(lifecycle, "SINKS", (lifecycle.Sink("one", lambda e: None, waited.append),))
    lifecycle.flush()
    assert waited == [None]


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


def test_turning_usage_stats_off_replaces_a_value_on_more_than_one_line(tmp_path):
    path = tmp_path / config.PROJECT_FILE
    path.write_text(f"apps: []\n{config.USAGE_STATS_KEY}:\n  true\n\nalerts: []\n")
    config.save_usage_stats_off(path)
    assert config.load_yaml(path) == {"apps": [], "alerts": [], config.USAGE_STATS_KEY: False}


def test_turning_usage_stats_off_leaves_a_file_it_cannot_change_as_it_was(tmp_path):
    path = tmp_path / config.PROJECT_FILE
    path.write_text("apps: []\n...\n")
    with pytest.raises(config.ConfigError, match=config.USAGE_STATS_KEY):
        config.save_usage_stats_off(path)
    assert path.read_text() == "apps: []\n...\n"
