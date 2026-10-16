import builtins
import io
import sys
from datetime import UTC, datetime

import pytest
from rich.cells import cell_len
from rich.console import Console
from rich.spinner import Spinner

from conftest import add_app
from pdt import alerts_cli, cli, console, deploy, runs_cli
from pdt.deploy_common import CostEstimate

URL = ("https://portal.azure.com/#@contoso.onmicrosoft.com/resource/subscriptions/"
       "3f7c321a-ccc7-41b0-9b6b-f5fd14bef70a/resourceGroups/pdt-reports/overview")
IMAGE = "pdtacr3f7c321accc741b0.azurecr.io/pdt-quarterly-license-report:20261015T101530"
COMMAND = "pdt logs quarterly-license-report --run pdt-quarterly-license-report-29384756"


def screen(monkeypatch, columns: int, terminal: bool = True) -> io.StringIO:
    out = io.StringIO()
    monkeypatch.setattr(console, "_console", Console(
        file=out, width=columns, force_terminal=terminal, color_system=None, highlight=False,
        soft_wrap=True, emoji=False))
    return out


def assert_fits(text: str, columns: int) -> None:
    """Every line fits, except a line that holds only a URL or a command, which the terminal
    wraps."""
    for line in text.splitlines():
        assert cell_len(line) <= columns or " " not in line.strip() or line.strip() == COMMAND, line


def deploy_plan() -> None:
    v = console.value
    job = "pdt-quarterly-license-report"
    deploy.confirm([
        console.Markup(f"use existing ACR {v('pdtacr3f7c321accc741b0b6b2f5fd14bef70a')} (Basic)"),
        console.Markup(f"create lock {v(job)} on the Container Apps environment (keeps it "
                       f"while {v(job)} is deployed)"),
        "ensure scoped Key Vault secret permissions for the deployer (Key Vault Secrets Officer)",
        console.Markup(f"build and push image {v(IMAGE)} with ACR Tasks"),
        console.Markup(f'create Container Apps Job {v(job)}: "{v("0 6 * * 1")}" (UTC)'),
    ], True, CostEstimate(
        [("Container Apps Jobs: 4 runs x 5 min assumed, 0.5 vCPU / 1 GiB", 0.07),
         ("ACR Basic registry (shared by every app in the project)", 4.83)],
        "eastus2 list prices converted at the ECB reference rate, before free grants",
        "Not included: data transfer out of Azure, and the shared Container Apps environment.",
        "EUR"))


def messages() -> None:
    console.field("Log", URL)
    console.error(f"{console.value('report/config.yml')}: {console.value('schedule')} must be a "
                  "cron expression with five fields, such as \"0 6 * * 1\" for every Monday.")
    console.note("this login cannot read the data store yet; the deploy grants it the role")
    console.log_line("10:15:30", "WARNING", "Skipped user someone.with.a.long.name@contoso.com: "
                                            "no manager is set in Microsoft Entra ID")
    console.command(COMMAND, "read this run's log")
    console.next_steps([
        ("pdt logs quarterly-license-report --follow", "follow the newest run's log as it prints"),
        ("pdt health", "see whether each deployed app's last run succeeded"),
    ])


def tables() -> None:
    console.table(["name", "schedule", "platform", "enabled", "paused"],
                  [["quarterly-license-report", "0 6 * * 1", "google-cloud", "true", "false"]],
                  ["bold"])
    console.secret_changes([("updated", "MICROSOFT_GRAPH_CLIENT_SECRET", "abc1****", "xyz9****"),
                            ("new", "SLACK_WEBHOOK_URL", "", "http****")])
    started = datetime(2026, 10, 14, 20, 0, tzinfo=UTC)
    runs_cli.health({"quarterly-license-report": [
        runs_cli.Run("pdt-quarterly-license-report-850a747-29867040", started, started,
                     "succeeded", 0, 1)]}, False)


@pytest.mark.parametrize("columns", [60, 100])
@pytest.mark.parametrize("render", [deploy_plan, messages, tables])
def test_a_screen_fits_the_terminal(monkeypatch, columns, render):
    out = screen(monkeypatch, columns)
    render()
    assert_fits(out.getvalue(), columns)


def test_a_long_url_path_or_value_stays_whole_on_its_own_line(monkeypatch):
    out = screen(monkeypatch, 60)
    messages()
    deploy_plan()
    lines = out.getvalue().splitlines()
    assert lines[:2] == ["Log:", URL]
    assert IMAGE in lines
    assert '    "0 6 * * 1" (UTC)' in lines
    assert lines[lines.index(f"  {COMMAND}") + 1] == "      read this run's log"


def test_a_wrapped_line_starts_under_the_text_after_its_label(monkeypatch):
    out = screen(monkeypatch, 60)
    console.error("one two three four five six seven eight nine ten eleven twelve thirteen")
    console.bullet("create lock pdt-report on the Container Apps environment (keeps it)")
    assert out.getvalue().splitlines() == [
        "error: one two three four five six seven eight nine ten",
        "       eleven twelve thirteen",
        "  create lock pdt-report on the Container Apps environment",
        "    (keeps it)",
    ]


def test_a_word_too_wide_for_the_line_is_never_cut(monkeypatch):
    out = screen(monkeypatch, 30)
    console.bullet("x" * 40 + " tail")
    console.detail("q" * 40 + " " + "r" * 40 + " w")
    console.cost([("Supercalifragilistic-expialidocious 1 vCPU", 1.0)], "p")
    assert out.getvalue().splitlines() == [
        "x" * 40, "    tail",
        "q" * 40, "r" * 40, "      w",
        "Estimated monthly cost (p):",
        "  Supercalifragilistic-expialidocious  $      1.00",
        "    1 vCPU",
        "  total            $USD   1.00",
    ]


def test_a_table_too_wide_prints_one_block_per_row(monkeypatch):
    out = screen(monkeypatch, 40)
    console.table(["#", "Started", "Status", "Id"],
                  [["1", "2026-10-14 20:00:00", "succeeded", "pdt-report-850a747-29867040"]])
    assert out.getvalue().splitlines() == [
        "1",
        "  Started: 2026-10-14 20:00:00",
        "  Status: succeeded",
        "  Id: pdt-report-850a747-29867040",
    ]


def test_a_cost_label_wraps_and_keeps_the_amounts_in_one_column(monkeypatch):
    out = screen(monkeypatch, 40)
    console.cost([("Container Apps Jobs: 4 runs x 5 min assumed", 0.07), ("Key Vault", 0.01)],
                 "list prices")
    assert out.getvalue().splitlines() == [
        "Estimated monthly cost (list prices):",
        "  Container Apps Jobs: 4     $      0.07",
        "    runs x 5 min assumed",
        "  Key Vault                  $      0.01",
        "  total                      $USD   0.08",
    ]


@pytest.mark.parametrize("columns", [60, 100])
def test_a_question_and_its_checkboxes_fit_the_terminal(monkeypatch, columns):
    out = screen(monkeypatch, columns)
    monkeypatch.setattr(builtins, "input", lambda *_: "")
    alerts_cli.checkboxes("Which events should go to Slack?",
                          [(event, f"Run completed ({event})") for event in alerts_cli.EVENTS],
                          alerts_cli.EVENTS[:2])
    assert_fits(out.getvalue(), columns)
    assert out.getvalue().splitlines()[-1].endswith("on: ")


@pytest.mark.parametrize("columns", [60, 100])
def test_pdt_help_fits_the_terminal(monkeypatch, capsys, columns):
    monkeypatch.setenv("COLUMNS", str(columns))
    monkeypatch.setattr(cli.HelpFormatter, "console", property(lambda self: Console(
        width=columns, force_terminal=True, color_system=None, file=sys.stdout)))
    cli.build_parser().print_help()
    assert_fits(capsys.readouterr().out, columns)


def test_a_mistyped_command_wraps_its_error_at_a_space(monkeypatch, capsys):
    monkeypatch.setattr(sys.stderr, "isatty", lambda: True)
    monkeypatch.setattr(console, "width", lambda: 60)
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["lgos"])
    error = capsys.readouterr().err
    assert_fits(error, 60)
    assert "`pdt logs`" in error


def test_pdt_list_fits_the_terminal(project, monkeypatch):
    add_app(project, "quarterly-license-report-for-finance", "schedule: \"0 6 * * 1\"\n")
    out = screen(monkeypatch, 60)
    monkeypatch.setattr(sys, "argv", ["pdt", "list"])
    assert cli.main() == 0
    assert out.getvalue().splitlines()[0] == "quarterly-license-report-for-finance"
    assert_fits(out.getvalue(), 60)


def test_a_wait_stays_on_one_line(monkeypatch):
    screen(monkeypatch, 40)
    wait = console.Wait("Waiting for ACR Tasks to build pdt-quarterly-license-report")
    lines = console._console.render_lines(Spinner("dots", text=wait), pad=False)
    assert len(lines) == 1


def test_progress_never_wraps(monkeypatch):
    out = screen(monkeypatch, 20)
    console.progress("  1024 / 2048 MB downloaded so far")
    assert out.getvalue() == "  1024 / 2048 MB do\r"
    console.progress("日本" * 30)
    assert cell_len(out.getvalue().split("\r")[1]) <= 19


def test_output_that_is_not_a_terminal_does_not_wrap(monkeypatch):
    out = screen(monkeypatch, 40, terminal=False)
    console.error("one two three four five six seven eight nine ten eleven twelve")
    console.table(["#", "Started", "Status", "Id"],
                  [["1", "2026-10-14 20:00:00", "succeeded", "pdt-report-850a747-29867040"]])
    console.cost([("Container Apps Jobs: 4 runs x 5 min assumed", 0.07)], "list prices")
    assert out.getvalue().splitlines() == [
        "error: one two three four five six seven eight nine ten eleven twelve",
        "#  Started              Status     Id",
        "1  2026-10-14 20:00:00  succeeded  pdt-report-850a747-29867040",
        "Estimated monthly cost (list prices):",
        "  Container Apps Jobs: 4 runs x 5 min assumed  $      0.07",
        "  total                                        $USD   0.07",
    ]
