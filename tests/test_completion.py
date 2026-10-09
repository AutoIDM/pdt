import os
import shutil
import subprocess
import sys

import pytest

from pdt import cli, completion, config

from conftest import add_app


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


def _complete(line, tmp_path):
    output = tmp_path / "completions"
    environment = {**os.environ, "_ARGCOMPLETE": "1", "_ARGCOMPLETE_IFS": "\n",
                   "_ARGCOMPLETE_STDOUT_FILENAME": str(output),
                   "COMP_LINE": line, "COMP_POINT": str(len(line))}
    subprocess.run([sys.executable, "-c", "from pdt import cli; cli.main()"],
                   env=environment, stdin=subprocess.DEVNULL, capture_output=True)
    return output.read_text().split("\n")


def test_first_tab_offers_app_names_without_options(project, tmp_path):
    add_app(project, "daily-report")
    add_app(project, "weekly-report")

    assert _complete("pdt deploy ", tmp_path) == ["daily-report", "weekly-report"]


def test_positionals_that_are_not_paths_offer_no_file_names(project, tmp_path):
    add_app(project, "daily-report")
    (project / "notes.txt").write_text("")

    assert _complete("pdt logs ", tmp_path) == ["daily-report "]
    assert _complete("pdt logs daily-report ", tmp_path) == [""]
    assert _complete("pdt new ", tmp_path) == [""]


def test_app_names_outside_a_project_offer_no_file_names(tmp_path, monkeypatch):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "notes.txt").write_text("")
    monkeypatch.chdir(elsewhere)

    assert _complete("pdt run ", tmp_path) == [""]
    assert _complete("pdt logs ", tmp_path) == [""]


def test_path_arguments_still_offer_file_names(project, tmp_path):
    add_app(project, "daily-report")
    (project / "notes.txt").write_text("")

    assert _complete("pdt storage daily-report ", tmp_path) == ["ls", "get", "query", "unlock", "destroy"]
    assert "notes.txt" in _complete("pdt storage daily-report get report.csv ", tmp_path)
    assert "notes.txt" in _complete("pdt aws s3 cp ", tmp_path)


def test_typed_dash_offers_options(project, tmp_path):
    add_app(project, "daily-report")

    assert _complete("pdt deploy -", tmp_path) == ["-h", "--help", "--yes", "--all", "--skip-failures"]


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


def test_setup_writes_zsh_registration(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "data"))
    monkeypatch.setenv("ZDOTDIR", str(tmp_path / "zsh"))
    monkeypatch.setattr(completion, "_shell", lambda: "zsh")

    completion.setup()
    startup = tmp_path / "zsh" / ".zshrc"
    first = startup.read_text()
    completion.setup()

    assert "pdt.zsh" in first
    assert "$+functions[compdef]" in (tmp_path / "data" / "pdt" / "pdt.zsh").read_text()
    assert startup.read_text() == first


def test_setup_writes_bash_interactive_and_login_registration(tmp_path, monkeypatch):
    profile = tmp_path / ".profile"
    profile.write_text("existing\n")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "data"))
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


def test_setup_uses_configured_powershell_profile(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "data"))
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


def test_install_command_sets_up_a_named_shell(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "data"))
    monkeypatch.delenv("ZDOTDIR", raising=False)
    monkeypatch.setattr(completion, "_shell", lambda: None)

    assert completion.install("zsh", print_only=False) == 0

    assert "pdt.zsh" in (tmp_path / ".zshrc").read_text()
    out = capsys.readouterr().out
    assert str(tmp_path / ".zshrc") in out
    assert 'eval "$(pdt completion zsh --script)"' in out


def test_install_command_with_unknown_shell_names_the_choices(monkeypatch, capsys):
    monkeypatch.setattr(completion, "_shell", lambda: None)

    assert completion.install(None, print_only=False) == 1
    out = capsys.readouterr().out
    for shell in completion.SHELLS:
        assert f"pdt completion {shell}" in out


def test_script_flag_prints_without_writing(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))

    assert completion.install("bash", print_only=True) == 0

    assert "pdt" in capsys.readouterr().out
    assert not (tmp_path / ".bashrc").exists()
    assert not (tmp_path / "data").exists()


class FakePowerShell:
    def __init__(self, *policies):
        self.policies = list(policies)
        self.commands = []

    def __call__(self, shell, command):
        self.commands.append(command)
        stdout = self.policies.pop(0) + "\n" if command == "Get-ExecutionPolicy" else ""
        return subprocess.CompletedProcess([shell], 0, stdout, "")


def test_powershell_profile_needs_nothing_under_remote_signed(monkeypatch, capsys):
    fake = FakePowerShell("RemoteSigned")
    monkeypatch.setattr(completion, "_powershell", fake)

    completion._allow_profile("powershell")

    assert fake.commands == ["Get-ExecutionPolicy"]
    assert "every new PowerShell window" in capsys.readouterr().out


def test_restricted_policy_without_a_terminal_prints_the_fix(monkeypatch, capsys):
    fake = FakePowerShell("Restricted")
    monkeypatch.setattr(completion, "_powershell", fake)
    monkeypatch.setattr(completion, "can_prompt", lambda interactive: False)

    completion._allow_profile("powershell")

    assert fake.commands == ["Get-ExecutionPolicy"]
    out = capsys.readouterr().out
    assert "(Restricted)" in out
    assert completion.ALLOW_PROFILE in out


def test_restricted_policy_is_changed_after_a_yes(monkeypatch, capsys):
    fake = FakePowerShell("Restricted", "RemoteSigned")
    monkeypatch.setattr(completion, "_powershell", fake)
    monkeypatch.setattr(completion, "can_prompt", lambda interactive: True)
    monkeypatch.setattr(completion.console, "confirm", lambda question: True)

    completion._allow_profile("powershell")

    assert fake.commands == ["Get-ExecutionPolicy", f"{completion.ALLOW_PROFILE} -Force",
                             "Get-ExecutionPolicy"]
    assert "now RemoteSigned" in capsys.readouterr().out


def test_a_group_policy_that_keeps_the_policy_is_named(monkeypatch, capsys):
    fake = FakePowerShell("Restricted", "Restricted")
    monkeypatch.setattr(completion, "_powershell", fake)
    monkeypatch.setattr(completion, "can_prompt", lambda interactive: True)
    monkeypatch.setattr(completion.console, "confirm", lambda question: True)

    completion._allow_profile("powershell")

    assert "Group Policy" in capsys.readouterr().out


@pytest.mark.parametrize(("shell", "line"), [
    ("bash", 'eval "$(pdt completion bash --script)"'),
    ("zsh", 'eval "$(pdt completion zsh --script)"'),
    ("fish", "pdt completion fish --script | source"),
    ("powershell", "pdt completion powershell --script | Out-String | Invoke-Expression"),
    ("pwsh", "pdt completion pwsh --script | Out-String | Invoke-Expression"),
])
def test_this_window_line_loads_the_script_without_a_file(capsys, shell, line):
    completion._this_window(shell)

    assert line in capsys.readouterr().out


@pytest.mark.parametrize(("shell", "check"), [
    ("bash", "complete -p pdt"),
    ("zsh", "(( ${+_comps[pdt]} ))"),
])
def test_the_eval_line_registers_completion_in_a_shell_with_no_startup_file(shell, check):
    if shutil.which(shell) is None:
        pytest.skip(f"needs {shell}")
    if os.name == "nt":
        pytest.skip("bash.exe on a Windows runner is the WSL launcher, with no Linux installed")
    start = ["bash", "--norc", "--noprofile", "-c"] if shell == "bash" else ["zsh", "-f", "-c"]
    result = subprocess.run([*start, f'eval "$PDT_SCRIPT"; {check}'], capture_output=True,
                            text=True, env={**os.environ, "PDT_SCRIPT": completion._script(shell)})

    assert result.returncode == 0, result.stderr


def test_bash_does_not_fall_back_to_file_names():
    assert "-o default" not in completion._script("bash")


def test_powershell_on_windows_sets_up_both_profiles(tmp_path, monkeypatch):
    def paths(shell):
        return tmp_path / "pdt.ps1", tmp_path / f"{shell}.ps1"

    monkeypatch.setattr(completion, "_paths", paths)
    monkeypatch.setattr(completion.os, "name", "nt")
    completion.setup("powershell")

    assert (tmp_path / "powershell.ps1").is_file()
    assert (tmp_path / "pwsh.ps1").is_file()


TAB_IN_POWERSHELL = r"""
$env:PDT_SCRIPT | Out-String | Invoke-Expression
foreach ($folder in '.', 'daily-report', $env:ELSEWHERE) {
    Push-Location $folder
    foreach ($line in 'pdt run ', 'pdt logs ') {
        $found = (TabExpansion2 -inputScript $line -cursorColumn $line.Length).CompletionMatches
        "[" + ($found.CompletionText -join ',') + "]"
    }
    Pop-Location
}
"""


@pytest.mark.parametrize("shell", ["pwsh", "powershell"])
def test_powershell_tab_completes_app_names(project, tmp_path_factory, shell):
    if shutil.which(shell) is None or shutil.which("pdt") is None:
        pytest.skip(f"needs {shell} and pdt on the PATH")
    add_app(project, "daily-report")
    add_app(project, "weekly-report")
    elsewhere = tmp_path_factory.mktemp("elsewhere")
    (elsewhere / "notes.txt").write_text("")
    # Windows PowerShell started from pwsh inherits pwsh's PSModulePath and then
    # cannot load its own New-TemporaryFile, which the completion script calls.
    environment = {key: value for key, value in os.environ.items()
                   if key.upper() != "PSMODULEPATH"}
    result = subprocess.run(
        [shell, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", TAB_IN_POWERSHELL],
        env={**environment, "PDT_SCRIPT": completion._script(shell), "ELSEWHERE": str(elsewhere)},
        stdin=subprocess.DEVNULL, capture_output=True, text=True)

    assert result.stdout.split() == ["[daily-report,weekly-report]"] * 4 + ["[]"] * 2, result.stderr
