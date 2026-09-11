import pytest

from conftest import add_app
from pdt import cli, completion


def complete(*words, cword=None):
    """Complete the last word of `pdt words...`; an empty last word means Tab after a space."""
    words = ["pdt", *words]
    if cword is None:
        cword = len(words) - 1
    return completion.candidates(cli.build_parser(), cword, words)


@pytest.mark.parametrize("command", ["run", "deploy", "destroy", "login"])
def test_app_name_completes_from_a_prefix(project, command):
    add_app(project, "bamboohr2azure")
    add_app(project, "hello-world")
    assert complete(command, "bamboohr") == ["bamboohr2azure"]
    assert complete(command, "") == ["bamboohr2azure", "hello-world"]


def test_app_completes_after_an_option_and_its_value(project):
    add_app(project, "bamboohr2azure")
    assert complete("deploy", "--yes", "bam") == ["bamboohr2azure"]
    assert complete("deploy", "--profile", "work", "bam") == ["bamboohr2azure"]
    # The value of --profile is free text, so Tab offers nothing there.
    assert complete("deploy", "--profile", "") == []


def test_second_positional_offers_nothing(project):
    add_app(project, "bamboohr2azure")
    assert complete("run", "bamboohr2azure", "") == []


def test_options_complete_for_a_command():
    assert complete("deploy", "--") == ["--help", "--yes", "--profile"]
    assert complete("deploy", "--y") == ["--yes"]
    assert complete("--v") == ["--version"]


def test_commands_complete_and_hide_the_tab_handler():
    names = complete("")
    assert "run" in names and "deploy" in names and "completion" in names
    assert "_complete" not in names
    assert complete("ru") == ["run"]


def test_new_from_completes_example_names():
    assert "hello-world" in complete("new", "my-report", "--from", "")
    assert complete("new", "my-report", "--from", "hello") == ["hello-world"]


def test_shell_names_complete_from_argparse_choices():
    assert complete("completion", "") == list(completion.SHELLS)


def test_cloud_cli_passthrough_and_unknown_command_offer_nothing():
    assert complete("aws", "s") == []
    assert complete("nonsense", "") == []


def test_outside_a_project_offers_no_apps_and_no_error(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("PDT_PROJECT", raising=False)
    monkeypatch.chdir(tmp_path)
    assert complete("run", "") == []
    monkeypatch.setattr("sys.argv", ["pdt", "_complete", "2", "pdt", "run", ""])
    assert cli.main() == 0
    assert capsys.readouterr().out == ""


def test_complete_command_prints_one_name_per_line(project, monkeypatch, capsys):
    add_app(project, "bamboohr2azure")
    add_app(project, "hello-world")
    monkeypatch.setattr("sys.argv", ["pdt", "_complete", "2", "pdt", "run"])
    assert cli.main() == 0
    assert capsys.readouterr().out == "bamboohr2azure\nhello-world\n"


def test_complete_never_raises_on_bad_input(capsys):
    assert completion.complete(cli.build_parser(), []) == 0
    assert completion.complete(cli.build_parser(), ["x"]) == 0
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("shell", completion.SHELLS)
def test_every_shell_script_calls_the_tab_handler(shell):
    assert "_complete" in completion.script(shell)


def test_install_writes_one_block_and_replaces_it_on_rerun(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("SHELL", "/bin/bash")
    monkeypatch.delenv("ZDOTDIR", raising=False)
    rc = tmp_path / ".bashrc"
    rc.write_text("export EDITOR=vi\n")
    assert completion.install(None, print_only=False) == 0
    assert completion.install("bash", print_only=False) == 0
    text = rc.read_text()
    assert text.startswith("export EDITOR=vi\n")
    assert text.count(completion.BEGIN) == 1
    assert text.count(completion.END) == 1
    assert "complete -F _pdt_complete pdt" in text
    out = capsys.readouterr().out
    assert str(rc) in out
    assert f"source {rc}" in out


def test_install_for_fish_writes_an_autoloaded_file(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    assert completion.install("fish", print_only=False) == 0
    path = tmp_path / ".config" / "fish" / "completions" / "pdt.fish"
    assert path.read_text() == completion.script("fish")


def test_install_with_unknown_shell_names_the_choices(monkeypatch, capsys):
    monkeypatch.setenv("SHELL", "/bin/tcsh")
    monkeypatch.setattr("os.name", "posix")
    assert completion.install(None, print_only=False) == 1
    out = capsys.readouterr().out
    for shell in completion.SHELLS:
        assert f"pdt completion {shell}" in out


def test_script_flag_prints_without_writing(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HOME", str(tmp_path))
    assert completion.install("zsh", print_only=True) == 0
    assert capsys.readouterr().out == completion.script("zsh")
    assert not (tmp_path / ".zshrc").exists()


def test_completion_is_in_the_cli_help():
    # The command list lives in argparse's own help, not in the module docstring.
    assert "completion" in cli.build_parser().format_help()
    assert "completion" in cli.__doc__


@pytest.fixture
def offer_ready(tmp_path, monkeypatch):
    """A bash user with a terminal, an empty ~/.bashrc, and no remembered answer."""
    monkeypatch.setattr(completion, "can_ask", lambda: True)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("SHELL", "/bin/bash")
    monkeypatch.delenv("ZDOTDIR", raising=False)
    return tmp_path / ".bashrc"


def answer_with(monkeypatch, text):
    asked = []

    def fake_input(prompt):
        asked.append(prompt)
        return text

    monkeypatch.setattr("builtins.input", fake_input)
    return asked


def refuse_input(_prompt=""):
    raise AssertionError("no question should have been asked")


def test_offer_installs_on_enter_and_remembers(offer_ready, monkeypatch, capsys):
    asked = answer_with(monkeypatch, "")
    completion.offer()
    assert asked == ["Turn on tab completion for pdt in bash? [Y/n] "]
    assert completion.BEGIN in offer_ready.read_text()
    assert completion.answer_file().read_text() == "yes\n"
    assert "for this one, run: source" in capsys.readouterr().out
    monkeypatch.setattr("builtins.input", refuse_input)
    completion.offer()


def test_offer_remembers_a_no_and_never_asks_again(offer_ready, monkeypatch, capsys):
    answer_with(monkeypatch, "n")
    completion.offer()
    assert not offer_ready.exists()
    assert completion.answer_file().read_text() == "no\n"
    assert "pdt completion" in capsys.readouterr().out
    monkeypatch.setattr("builtins.input", refuse_input)
    completion.offer()
    assert not offer_ready.exists()


def test_offer_skips_when_already_installed(offer_ready, monkeypatch):
    completion.write_block(offer_ready, "bash")
    monkeypatch.setattr("builtins.input", refuse_input)
    completion.offer()
    assert completion.answer_file().read_text() == "yes\n"


def test_offer_skips_with_yes_flag_or_no_terminal(offer_ready, monkeypatch):
    monkeypatch.setattr("builtins.input", refuse_input)
    completion.offer(assume_yes=True)
    monkeypatch.setattr(completion, "can_ask", lambda: False)
    completion.offer()
    assert not offer_ready.exists()
    assert not completion.answer_file().exists()


def test_offer_skips_an_unknown_shell(offer_ready, monkeypatch):
    monkeypatch.setenv("SHELL", "/bin/tcsh")
    monkeypatch.setattr("os.name", "posix")
    monkeypatch.setattr("builtins.input", refuse_input)
    completion.offer()
    assert not completion.answer_file().exists()


def test_manual_install_settles_the_question(offer_ready, monkeypatch):
    assert completion.install("bash", print_only=False) == 0
    monkeypatch.setattr("builtins.input", refuse_input)
    completion.offer()
    assert completion.answer_file().read_text() == "yes\n"


def test_init_offers_completion_at_the_end(offer_ready, tmp_path, monkeypatch, capsys):
    from pdt import scaffold

    monkeypatch.delenv("PDT_PROJECT", raising=False)
    monkeypatch.chdir(tmp_path)
    asked = answer_with(monkeypatch, "")
    assert scaffold.init("my-jobs", assume_yes=True) == 0
    assert asked == [], "--yes must not ask about completion"
    assert not offer_ready.exists()


def test_successful_run_offers_completion(project, offer_ready, monkeypatch):
    add_app(project, "hello-world")
    monkeypatch.setattr("subprocess.run", lambda *a, **k: type("P", (), {"returncode": 0})())
    monkeypatch.setattr("pdt.config.is_deployed", lambda _name: True)
    asked = answer_with(monkeypatch, "")
    monkeypatch.setattr("sys.argv", ["pdt", "run", "hello-world"])
    assert cli.main() == 0
    assert len(asked) == 1
    assert completion.BEGIN in offer_ready.read_text()


def test_failed_run_does_not_offer(project, offer_ready, monkeypatch):
    add_app(project, "hello-world")
    monkeypatch.setattr("subprocess.run", lambda *a, **k: type("P", (), {"returncode": 1})())
    monkeypatch.setattr("builtins.input", refuse_input)
    monkeypatch.setattr("sys.argv", ["pdt", "run", "hello-world"])
    assert cli.main() == 1
    assert not completion.answer_file().exists()
