import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from pdt import notify, runs_cli
from pdt.runs_cli import Run
from pdt.utils import send_email
from pdt.utils.log import die


@pytest.fixture(autouse=True)
def resend(monkeypatch):
    for name in send_email.EMAIL_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("LOG_FORMAT", raising=False)
    monkeypatch.delenv("CLOUD_RUN_JOB", raising=False)
    monkeypatch.setenv("PDT_RESEND_API_KEY", "re_1")


def app(on_failure=["email"], to="ops@example.com", email_from="pdt@example.com"):
    return {"name": "my-report", "platform": {"provider": "azure"},
            "config": {"email_from": email_from}, "on_failure": on_failure,
            "notify": {"email": {"to": to}}}


def log_lines(capsys) -> list[str]:
    return [re.sub(r"^\d\d:\d\d:\d\d ", "", line)
            for line in capsys.readouterr().out.splitlines()]


def test_notice_has_the_job_details():
    assert notify.notice_text("my-report", "azure", 3,
                              "2026-10-07T14:03:41Z", "2026-10-07T14:04:43Z", 62) == (
        "App: my-report\nProvider: azure\n\n"
        "The job started at 2026-10-07T14:03:41Z and failed with exit code 3 after 62 seconds.\n"
        "It ended at 2026-10-07T14:04:43Z.\n\nTo read its log:\n"
        "pdt logs my-report --failed --since 2026-10-07T13:58Z --span 10m\n")


def test_failure_sends_an_email(monkeypatch, capsys):
    sent = []
    monkeypatch.setattr(notify, "send_email", lambda *args: sent.append(args))

    notify.notify_failure(app(), 3, "2026-10-07T10:00:00Z", "end", 2)

    assert log_lines(capsys) == []
    assert sent == [("pdt@example.com", "ops@example.com", "pdt: my-report failed",
                     notify.notice_text("my-report", "azure", 3, "2026-10-07T10:00:00Z", "end", 2))]


def test_success_does_not_send_an_email(monkeypatch):
    monkeypatch.setattr(notify, "send_email", lambda *args: (_ for _ in ()).throw(AssertionError()))
    notify.notify_failure(app(), 0, "2026-10-07T10:00:00Z", "end", 2)


def test_transport_failure_keeps_the_exit_code_and_logs_one_line(monkeypatch, capsys):
    monkeypatch.setattr(notify, "send_email", lambda *args: (_ for _ in ()).throw(OSError("down")))

    notify.notify_failure(app(), 7, "2026-10-07T10:00:00Z", "end", 2)

    assert log_lines(capsys) == ["WARNING failure notice not sent: down"]


def test_a_transport_that_exits_logs_its_reason_and_one_line(monkeypatch, capsys):
    monkeypatch.setattr(notify, "send_email",
                        lambda *args: die(3, "resend send failed", status=401))

    notify.notify_failure(app(), 7, "2026-10-07T10:00:00Z", "end", 2)

    lines = log_lines(capsys)
    assert lines[-1] == ("WARNING failure notice not sent: the send failed; "
                         "the ERROR line above names the cause")
    assert all(line == "ERROR   resend send failed  status=401" for line in lines[:-1])


@pytest.mark.parametrize(("notice_app", "unset", "reason"), [
    (app(on_failure=[]), None, "on_failure in config.yml does not list email"),
    (app(to=None), None, "notify.email.to is not set in config.yml"),
    (app(to=[]), None, "notify.email.to is not set in config.yml"),
    (app(email_from=""), None,
     "email_from is required when sending email; set it in config.yml"),
    (app(), "PDT_RESEND_API_KEY",
     "notify.email.to asks for a failure email, but no email option is set; add PDT_SMTP_HOST, "
     "PDT_SES_REGION, PDT_RESEND_API_KEY, or PDT_GRAPH_MAIL_USER to the .env file"),
])
def test_a_skipped_email_logs_one_line_with_the_reason(monkeypatch, capsys, notice_app, unset,
                                                       reason):
    if unset is not None:
        monkeypatch.delenv(unset)
    monkeypatch.setattr(notify, "send_email", lambda *args: pytest.fail("sent"))

    notify.notify_failure(notice_app, 3, "2026-10-07T10:00:00Z", "end", 2)

    assert log_lines(capsys) == [f"WARNING failure notice not sent: {reason}"]


def test_an_email_that_never_answers_logs_the_timeout(monkeypatch, capsys):
    class Silent:
        def wait(self, timeout):
            return False

        def set(self):
            pass

    monkeypatch.setattr(notify, "Event", Silent)
    monkeypatch.setattr(notify, "Thread", lambda target, daemon: type(
        "Idle", (), {"start": lambda self: None})())

    notify.notify_failure(app(), 3, "2026-10-07T10:00:00Z", "end", 2)

    assert log_lines(capsys) == [
        "WARNING failure notice not sent: the email server did not answer within 10 seconds"]


def test_send_retries_after_a_timeout(monkeypatch):
    calls = []

    class Done:
        def wait(self, timeout):
            calls.append(timeout)
            return len(calls) > 1

        def set(self):
            pass

    class Worker:
        def __init__(self, target, daemon):
            self.target = target

        def start(self):
            if len(calls) > 0:
                self.target()

    monkeypatch.setattr(notify, "Event", Done)
    monkeypatch.setattr(notify, "Thread", Worker)
    monkeypatch.setattr(notify, "send_email", lambda *args: None)

    assert notify.send_with_retry("from@example.com", "to@example.com", "subject", "body") == ""
    assert calls == [10, 10]


def notice_window(started: str) -> tuple[datetime, timedelta]:
    args = notice_command(started).split()
    since = runs_cli.parse_since(args[args.index("--since") + 1], datetime.now(UTC))
    return since, runs_cli.parse_amount(args[args.index("--span") + 1])


def notice_command(started: str) -> str:
    return notify.notice_text("my-report", "azure", 3, started, "end", 2).splitlines()[-1]


def test_notice_window_rounds_down_to_the_minute_5_minutes_before_the_start():
    assert notice_window("2026-10-07T14:03:59Z") == (
        datetime(2026, 10, 7, 13, 58, tzinfo=UTC), timedelta(minutes=10))
    assert notice_window("2026-10-07T14:00:00Z")[0] == datetime(2026, 10, 7, 13, 55,
                                                                   tzinfo=UTC)


def test_notice_window_holds_a_provider_start_before_the_container_start():
    since, span = notice_window("2026-10-07T14:03:41Z")
    job = Run("job-1", datetime(2026, 10, 7, 14, 1, 41, tzinfo=UTC), None, "failed", 3)
    assert runs_cli.window([job], since, span, 10) == [job]


def test_the_notice_can_save_a_renewed_token_in_every_cloud_secret():
    header = Path(notify.__file__).read_text().split("# ///")[1]
    for package in ("boto3", "azure-identity", "azure-keyvault-secrets",
                    "google-cloud-secret-manager"):
        assert f'"{package}"' in header
