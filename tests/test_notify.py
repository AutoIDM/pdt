from datetime import UTC, datetime, timedelta

from pdt import notify, runs_cli
from pdt.runs_cli import Run


def app(on_failure=["email"], to="ops@example.com"):
    return {"name": "my-report", "platform": {"provider": "azure"},
            "config": {"email_from": "pdt@example.com"}, "on_failure": on_failure,
            "notify": {"email": {"to": to}}}


def test_notice_has_the_job_details():
    assert notify.notice_text("my-report", "azure", 3,
                              "2026-10-07T14:03:41Z", "2026-10-07T14:04:43Z", 62) == (
        "App: my-report\nProvider: azure\n\n"
        "The job started at 2026-10-07T14:03:41Z and failed with exit code 3 after 62 seconds.\n"
        "It ended at 2026-10-07T14:04:43Z.\n\nTo read its log:\n"
        "pdt logs my-report --failed --since 2026-10-07T13:58Z --span 10m\n")


def test_failure_sends_an_email(monkeypatch):
    sent = []
    monkeypatch.setattr(notify, "pick_transport", lambda: "smtp")
    monkeypatch.setattr(notify, "send_email", lambda *args: sent.append(args))

    notify.notify_failure(app(), 3, "2026-10-07T10:00:00Z", "end", 2)

    assert sent == [("pdt@example.com", "ops@example.com", "pdt: my-report failed",
                     notify.notice_text("my-report", "azure", 3, "2026-10-07T10:00:00Z", "end", 2))]


def test_success_does_not_send_an_email(monkeypatch):
    monkeypatch.setattr(notify, "send_email", lambda *args: (_ for _ in ()).throw(AssertionError()))
    notify.notify_failure(app(), 0, "2026-10-07T10:00:00Z", "end", 2)


def test_transport_failure_keeps_the_exit_code(monkeypatch):
    monkeypatch.setattr(notify, "pick_transport", lambda: "smtp")
    monkeypatch.setattr(notify, "send_email", lambda *args: (_ for _ in ()).throw(OSError("down")))

    notify.notify_failure(app(), 7, "2026-10-07T10:00:00Z", "end", 2)


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

    assert notify.send_with_retry("from@example.com", "to@example.com", "subject", "body")
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
