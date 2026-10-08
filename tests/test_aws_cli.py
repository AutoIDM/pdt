import subprocess

from pdt import deploy_aws


def probe_answers(monkeypatch, code):
    probes = []

    def run(command, **kwargs):
        probes.append(command)
        return subprocess.CompletedProcess(command, code)

    monkeypatch.setattr(deploy_aws.subprocess, "run", run)
    return probes


def test_a_cached_aws_cli_runs_offline(monkeypatch):
    probes = probe_answers(monkeypatch, 0)
    assert deploy_aws.aws_cli() == ["uvx", "--offline", "--from", deploy_aws.AWS_CLI_V2, "aws"]
    assert "--offline" in probes[0]


def test_the_first_aws_cli_call_installs_it_online(monkeypatch):
    probe_answers(monkeypatch, 1)
    assert deploy_aws.aws_cli() == ["uvx", "--from", deploy_aws.AWS_CLI_V2, "aws"]


def test_the_first_aws_cli_call_says_it_installs(monkeypatch, capsys):
    probe_answers(monkeypatch, 1)
    deploy_aws.aws_cli()
    assert "Installing the AWS CLI" in capsys.readouterr().out


def test_a_cached_aws_cli_starts_without_a_word(monkeypatch, capsys):
    probe_answers(monkeypatch, 0)
    deploy_aws.aws_cli()
    assert capsys.readouterr().out == ""
