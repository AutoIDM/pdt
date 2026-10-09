import inspect

import pytest

from pdt import console, deploy_aws_batch, deploy_azure_container_apps, deploy_google_cloud
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


def test_a_deploy_names_the_pdt_commands_after_the_run_command(width, capsys):
    deployed_next_steps("my-report", "Start-ScheduledTask -TaskName 'pdt-my-report'")
    assert capsys.readouterr().out.splitlines()[1:] == [
        "Next steps:",
        "  Start-ScheduledTask -TaskName 'pdt-my-report'",
        "                        start a run now",
        "  pdt logs my-report    read the log of the newest run",
        "  pdt runs my-report    list the recent runs",
        "  pdt health my-report  show whether the last run succeeded",
    ]


@pytest.mark.parametrize("module", [deploy_aws_batch, deploy_azure_container_apps,
                                    deploy_google_cloud, deploy_windows])
def test_every_provider_ends_its_deploy_with_the_same_next_steps(module):
    source = inspect.getsource(module.deploy)
    assert source.count("deployed_next_steps(") == 1
    assert "console.field(" not in source
