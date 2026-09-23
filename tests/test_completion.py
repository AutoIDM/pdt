import os

import pytest

from pdt import cli, completion, config

from conftest import add_app


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "data"))


def test_app_completer_lists_project_apps(project):
    add_app(project, "daily-report")
    add_app(project, "weekly-report")

    assert completion.apps("daily") == ["daily-report"]


def test_app_completer_outside_project_returns_no_candidates(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    assert completion.apps("") == []


def test_example_completer_lists_matching_examples():
    assert completion.examples("hello") == ["hello-world"]


def test_build_parser_attaches_dynamic_completers():
    parser = cli.build_parser()
    commands = parser._subparsers._group_actions[0].choices

    assert commands["run"]._actions[1].completer is completion.apps
    assert commands["new"]._option_string_actions["--from"].completer is completion.examples


def test_install_preserves_content_and_replaces_owned_block(tmp_path):
    path = tmp_path / ".zshrc"
    path.write_text("before\n# pdt completion start\nold\n# pdt completion end\nafter\n")
    content = "# pdt completion start\nnew\n# pdt completion end\n"

    completion._install(path, content)
    first = path.read_text()
    completion._install(path, content)

    assert first == "before\n# pdt completion start\nnew\n# pdt completion end\nafter\n"
    assert path.read_text() == first


def test_install_preserves_symlink_and_permissions(tmp_path):
    target = tmp_path / "dotfiles" / "zshrc"
    target.parent.mkdir()
    target.write_text("existing\n")
    target.chmod(0o600)
    path = tmp_path / ".zshrc"
    path.symlink_to(target)

    completion._install(path, "# pdt completion start\nnew\n# pdt completion end\n")

    assert path.is_symlink()
    if os.name != "nt":
        assert target.stat().st_mode & 0o777 == 0o600
    assert target.read_text().startswith("existing\n")


def test_setup_writes_zsh_registration(tmp_path, home, monkeypatch):
    monkeypatch.setenv("ZDOTDIR", str(tmp_path / "zsh"))
    monkeypatch.setattr(completion, "_shell", lambda: "zsh")

    completion.setup()
    startup = tmp_path / "zsh" / ".zshrc"
    first = startup.read_text()
    completion.setup()

    assert "$+functions[compdef]" in first
    assert "source " in first
    assert (tmp_path / "data" / "pdt" / "pdt.zsh").is_file()
    assert startup.read_text() == first


def test_setup_writes_bash_interactive_and_login_registration(tmp_path, home, monkeypatch):
    profile = tmp_path / ".profile"
    profile.write_text("existing\n")
    monkeypatch.setattr(completion, "_shell", lambda: "bash")

    completion.setup()

    assert "pdt.bash" in (tmp_path / ".bashrc").read_text()
    assert profile.read_text().startswith("existing\n")
    assert "pdt.bash" in profile.read_text()


def test_setup_uses_fish_completion_directory(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setattr(completion, "_shell", lambda: "fish")

    completion.setup()

    script = tmp_path / "config" / "fish" / "completions" / "pdt.fish"
    assert "complete --command pdt" in script.read_text()


def test_setup_uses_configured_powershell_profile(tmp_path, home, monkeypatch):
    monkeypatch.setattr(completion, "_shell", lambda: "pwsh")

    completion.setup()

    _, profile = completion._paths("pwsh")
    assert ". '" in profile.read_text()
    assert "Register-ArgumentCompleter" in (tmp_path / "data" / "pdt" / "pdt.ps1").read_text()


def test_cmd_setup_writes_both_powershell_profiles(tmp_path, monkeypatch):
    def paths(shell):
        return tmp_path / "pdt.ps1", tmp_path / f"{shell}.ps1"

    monkeypatch.setattr(completion, "_shell", lambda: "cmd")
    monkeypatch.setattr(completion, "_paths", paths)

    completion.setup()

    assert (tmp_path / "powershell.ps1").is_file()
    assert (tmp_path / "pwsh.ps1").is_file()
    assert "pdt.bat" in (tmp_path / "pdt.ps1").read_text()


def test_completion_runs_before_cloud_dispatch(monkeypatch):
    class CompletionRequest(Exception):
        pass

    def request(_parser):
        raise CompletionRequest

    def dispatch(*_args, **_kwargs):
        raise AssertionError("completion request reached cloud dispatch")

    monkeypatch.setattr("sys.argv", ["pdt", "aws", "s3"])
    monkeypatch.setattr(completion, "configure", request)
    monkeypatch.setattr("subprocess.run", dispatch)

    with pytest.raises(CompletionRequest):
        cli.main()


def test_data_home_is_shared_with_gcloud_sdk(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))

    assert completion._data_dir() == tmp_path / "pdt"
    assert completion._data_dir() == config.data_home() / "pdt"


def test_install_command_sets_up_a_named_shell(tmp_path, home, monkeypatch, capsys):
    monkeypatch.delenv("ZDOTDIR", raising=False)
    monkeypatch.setattr(completion, "_shell", lambda: None)

    assert completion.install("zsh", print_only=False) == 0

    assert "pdt.zsh" in (tmp_path / ".zshrc").read_text()
    assert str(tmp_path / ".zshrc") in capsys.readouterr().out


def test_install_command_with_unknown_shell_names_the_choices(monkeypatch, capsys):
    monkeypatch.setattr(completion, "_shell", lambda: None)

    assert completion.install(None, print_only=False) == 1
    out = capsys.readouterr().out
    for shell in completion.SHELLS:
        assert f"pdt completion {shell}" in out


def test_script_flag_prints_without_writing(tmp_path, home, capsys):
    assert completion.install("bash", print_only=True) == 0

    assert "pdt" in capsys.readouterr().out
    assert not (tmp_path / ".bashrc").exists()
    assert not (tmp_path / "data").exists()
