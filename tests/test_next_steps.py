import inspect
import io

import pytest
from rich.console import Console

from pdt import console, deploy_aws_batch, deploy_azure_container_apps, deploy_google_cloud
from pdt import scaffold
from pdt import deploy_windows
from pdt.deploy_common import deployed_next_steps


@pytest.fixture
def width(monkeypatch):
    def set_width(columns: int) -> None:
        monkeypatch.setattr(console._console, "width", columns)
    set_width(80)
    return set_width


def test_commands_and_descriptions_line_up(width, capsys):
    console.next_steps([("pdt validate", "check the config"), ("pdt run my-report", "run it here")])
    assert capsys.readouterr().out.splitlines() == [
        "Next steps:",
        "  pdt validate       check the config",
        "  pdt run my-report  run it here",
    ]


def test_a_long_command_prints_alone_and_keeps_the_column(width, capsys):
    long = "pdt gcloud run jobs execute pdt-my-report --region us-central1 --project my-project"
    console.next_steps([(long, "start a run now"), ("pdt logs my-report", "read the log")])
    assert capsys.readouterr().out.splitlines() == [
        "Next steps:",
        f"  {long}",
        "                      start a run now",
        "  pdt logs my-report  read the log",
    ]


def test_a_narrow_terminal_puts_each_description_under_its_command(width, capsys):
    width(30)
    console.next_steps([("pdt health my-report", "show whether the last run succeeded")],
                       "Then run:")
    out = capsys.readouterr().out.splitlines()
    assert out == [
        "Then run:",
        "  pdt health my-report",
        "      show whether the last",
        "      run succeeded",
    ]
    assert all(line == line.rstrip() for line in out)


def test_a_command_with_no_description_has_no_trailing_space(width, capsys):
    console.next_steps([("cd my-project", ""), ("pdt examples", "see what else you can start from")])
    assert capsys.readouterr().out.splitlines()[1] == "  cd my-project"


def test_a_deploy_suggests_pdt_run_deployed_first(width, capsys):
    deployed_next_steps("my-report")
    assert capsys.readouterr().out.splitlines()[1:] == [
        "Next steps:",
        "  pdt run my-report --deployed  start a run now",
        "  pdt logs my-report            read the log of the newest run",
        "  pdt runs my-report            list the recent runs",
        "  pdt health my-report          show whether the last run succeeded",
    ]


@pytest.mark.parametrize("module", [deploy_aws_batch, deploy_azure_container_apps,
                                    deploy_google_cloud, deploy_windows])
def test_every_provider_ends_its_deploy_with_the_same_next_steps(module):
    source = inspect.getsource(module.deploy)
    assert source.count("deployed_next_steps(") == 1
    assert "console.field(" not in source


def test_init_lists_the_files_it_wrote_in_the_same_columns(width, tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("PDT_PROJECT", raising=False)
    monkeypatch.chdir(tmp_path)
    assert scaffold.init(None, assume_yes=True) == 0
    out = capsys.readouterr().out.splitlines()
    first = out.index("  pdt.yml       settings shared by every app")
    assert out[first:first + 6] == [
        "  pdt.yml       settings shared by every app",
        "  .env          secrets, never committed",
        "  .gitignore",
        "  AGENTS.md     how an AI agent should work in this project (CLAUDE.md points",
        "                here)",
        "  hello-world/  a working app to run and edit",
    ]


def test_a_value_in_a_description_prints_bold_on_a_terminal_and_plain_when_piped(monkeypatch):
    rows = [("pdt validate", f"check the {console.value('.env')} values")]
    for options, expected in (
            ({"force_terminal": True, "color_system": "standard"},
             "  \x1b[1mpdt validate\x1b[0m  \x1b[2mcheck the \x1b[0m\x1b[1m.env\x1b[0m"
             "\x1b[2m values\x1b[0m\n"),
            ({"force_terminal": False}, "  pdt validate  check the .env values\n")):
        out = io.StringIO()
        monkeypatch.setattr(console, "_console", Console(file=out, soft_wrap=True, width=80,
                                                         **options))
        console.columns(rows)
        assert out.getvalue() == expected
