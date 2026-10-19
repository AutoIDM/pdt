from types import SimpleNamespace

import pytest

from pdt import coding_agents, console


class FakeComputer:
    def __init__(self, monkeypatch, tmp_path, programs=("claude",), terminal=True,
                 consent="y", pick="1", models=None, reply=None):
        self.runs = []
        self.envs = []
        self.models = MODELS if models is None else models
        self.reply = reply
        self.asked = []
        self.questions = []
        self.consent, self.pick = consent, pick
        for agent in coding_agents.AGENTS:
            monkeypatch.delenv(agent.session_var, raising=False)
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(coding_agents, "SETTINGS", tmp_path / "settings.yml")
        monkeypatch.setattr(coding_agents.shutil, "which",
                            lambda name: f"/bin/{name}" if name in programs else None)
        monkeypatch.setattr(coding_agents, "can_prompt", lambda interactive: terminal)
        monkeypatch.setattr(coding_agents.subprocess, "run", self.run)
        monkeypatch.setattr(console, "confirm", self.confirm)
        monkeypatch.setattr(console, "ask", self.ask)

    def run(self, command, **kwargs):
        if command[1:] == ["models"]:
            return SimpleNamespace(returncode=0, stdout=self.models, stderr="")
        self.runs.append((command, kwargs.get("input")))
        self.envs.append(kwargs.get("env"))
        if self.reply is not None:
            raise self.reply
        return SimpleNamespace(returncode=0, stdout="the reply\n", stderr="")

    def confirm(self, question="Proceed?"):
        self.asked.append(question)
        return self.consent == "y"

    def ask(self, question, default=""):
        self.questions.append(question)
        if self.pick is None:
            raise EOFError
        return self.pick


MODELS = "Available models\n\nauto - Auto (default)\nfast-low - Fast Low\nslow-low - Slow Low\n"


def test_the_first_request_asks_for_consent_and_remembers_the_answer(monkeypatch, tmp_path, capsys):
    computer = FakeComputer(monkeypatch, tmp_path)
    assert coding_agents.ask("Why did it fail?", {"error text": "boom"}) == "the reply"
    assert computer.asked == ["Let pdt send requests to Claude Code?"]
    assert "this text: error text." in capsys.readouterr().out
    command, sent = computer.runs[0]
    assert command[:4] == ["/bin/claude", "-p", "--effort", "low"]
    assert sent == "Why did it fail?\n\n## error text\nboom"
    assert coding_agents.ask("Again?") == "the reply"
    assert computer.asked == ["Let pdt send requests to Claude Code?"]
    assert coding_agents.read_settings() == {"coding-agent-consent": {"claude": True}}


def test_a_no_is_stored_and_pdt_does_not_ask_again(monkeypatch, tmp_path):
    computer = FakeComputer(monkeypatch, tmp_path, consent="n")
    assert coding_agents.ask("Why?") is None
    assert coding_agents.ask("Why?") is None
    assert len(computer.asked) == 1
    assert computer.runs == []


def test_with_no_one_to_ask_pdt_sends_nothing(monkeypatch, tmp_path, capsys):
    computer = FakeComputer(monkeypatch, tmp_path, terminal=False)
    assert coding_agents.ask("Why?") is None
    assert computer.asked == [] and computer.runs == []
    assert "you have not allowed one yet" in capsys.readouterr().out
    assert not coding_agents.SETTINGS.exists()


def test_inside_a_coding_agent_pdt_prints_the_request_and_starts_no_agent(monkeypatch, tmp_path,
                                                                          capsys):
    computer = FakeComputer(monkeypatch, tmp_path, programs=("claude", "codex"))
    monkeypatch.setenv("CODEX_THREAD_ID", "abc")
    assert coding_agents.ask("Why did it fail?", {"command": "pdt run report"}) is None
    out = capsys.readouterr().out
    assert "pdt runs inside Codex" in out and "## command\npdt run report" in out
    assert computer.asked == [] and computer.runs == []


def test_pdt_asks_which_agent_only_when_more_than_one_can_answer(monkeypatch, tmp_path):
    computer = FakeComputer(monkeypatch, tmp_path, programs=("claude", "codex"), pick="2")
    assert coding_agents.ask("Why?") == "the reply"
    assert computer.questions == ["Which one should pdt use?"]
    assert computer.runs[0][0][:2] == ["/bin/codex", "exec"]
    coding_agents.ask("Why?")
    assert computer.questions == ["Which one should pdt use?"]
    assert coding_agents.read_settings()["coding-agent"] == "codex"


def test_an_input_that_ends_at_the_question_sends_nothing(monkeypatch, tmp_path):
    computer = FakeComputer(monkeypatch, tmp_path, programs=("claude", "codex"), pick=None)
    assert coding_agents.ask("Why?") is None
    assert computer.runs == []


def test_an_agent_the_user_refused_leaves_one_choice_and_no_question(monkeypatch, tmp_path):
    computer = FakeComputer(monkeypatch, tmp_path, programs=("claude", "codex"))
    coding_agents.SETTINGS.write_text("coding-agent-consent:\n  claude: false\n")
    coding_agents.ask("Why?")
    assert computer.questions == []
    assert computer.runs[0][0][0] == "/bin/codex"


def test_no_agent_on_the_path_sends_nothing(monkeypatch, tmp_path, capsys):
    computer = FakeComputer(monkeypatch, tmp_path, programs=())
    assert coding_agents.ask("Why?") is None
    assert "pdt found no coding agent" in capsys.readouterr().out
    assert computer.runs == []


def test_secret_values_of_the_env_files_never_reach_the_agent(monkeypatch, tmp_path):
    computer = FakeComputer(monkeypatch, tmp_path)
    (tmp_path / ".env").write_text("API_TOKEN=s3cr3t-value\nDEBUG=1\n")
    coding_agents.ask("Why?", {"error text": "401 for s3cr3t-value, DEBUG=1"})
    assert computer.runs[0][1].endswith("401 for <secret removed>, DEBUG=1")


def test_cursor_runs_the_first_low_effort_model_of_the_account(monkeypatch, tmp_path):
    computer = FakeComputer(monkeypatch, tmp_path, programs=("cursor-agent",))
    coding_agents.ask("Why?")
    assert computer.runs[0][0] == ["/bin/cursor-agent", "-p", "--trust", "--mode", "ask",
                                  "--model", "fast-low"]


@pytest.mark.parametrize("name, flags", [("claude", ["--effort", "low"]),
                                         ("codex", ["-c", 'model_reasoning_effort="low"'])])
def test_each_agent_runs_at_low_effort(name, flags):
    agent = next(agent for agent in coding_agents.AGENTS if agent.name == name)
    command = agent.args(agent.program, {})
    assert any(command[index:index + 2] == flags for index in range(len(command)))


def test_with_no_one_to_ask_pdt_uses_the_one_agent_the_user_allowed(monkeypatch, tmp_path):
    computer = FakeComputer(monkeypatch, tmp_path, programs=("claude", "codex"), terminal=False)
    coding_agents.SETTINGS.write_text("coding-agent-consent:\n  claude: true\n")
    assert coding_agents.ask("Why?") == "the reply"
    assert computer.runs[0][0][0] == "/bin/claude"


def test_cursor_with_no_low_effort_model_is_not_asked(monkeypatch, tmp_path, capsys):
    computer = FakeComputer(monkeypatch, tmp_path, programs=("cursor-agent",),
                            models="auto - Auto\n")
    assert coding_agents.ask("Why?") is None
    assert computer.runs == []
    assert "lists no low-effort model for your account" in capsys.readouterr().out


def test_an_agent_that_does_not_answer_in_time_is_stopped(monkeypatch, tmp_path, capsys):
    FakeComputer(monkeypatch, tmp_path,
            reply=coding_agents.subprocess.TimeoutExpired("claude", coding_agents.REPLY_SECONDS))
    assert coding_agents.ask("Why?") is None
    assert "did not answer in 300 seconds" in capsys.readouterr().out


@pytest.mark.parametrize("text", ['coding-agent-consent: "false"\n',
                                  "coding-agent-consent:\n  claude: nope\n",
                                  "coding-agent:\n  - claude\n"])
def test_a_settings_file_of_the_wrong_shape_names_the_file_and_key(monkeypatch, tmp_path, text):
    FakeComputer(monkeypatch, tmp_path)
    coding_agents.SETTINGS.write_text(text)
    with pytest.raises(coding_agents.ConfigError, match="settings.yml: coding-agent"):
        coding_agents.ask("Why?")


def test_an_app_env_file_and_the_terminal_value_are_kept_from_the_agent(monkeypatch, tmp_path):
    computer = FakeComputer(monkeypatch, tmp_path)
    (tmp_path / "pdt.yml").write_text("platform:\n  provider: aws\n")
    (tmp_path / "report").mkdir()
    (tmp_path / "report" / ".env").write_text("API_TOKEN=file-value\n")
    monkeypatch.setenv("API_TOKEN", "terminal-value")
    monkeypatch.delenv("PDT_PROJECT", raising=False)
    coding_agents.ask("Why?", {"error text": "file-value and terminal-value"})
    assert computer.runs[0][1].endswith("<secret removed> and <secret removed>")
    assert "API_TOKEN" not in computer.envs[0]


def test_claude_runs_with_no_mcp_servers_and_no_hooks():
    command = coding_agents.AGENTS[0].args("claude", {})
    assert "--strict-mcp-config" in command
    assert command[command.index("--settings") + 1] == '{"disableAllHooks": true}'


def test_the_project_env_file_counts_when_pdt_project_names_the_project(monkeypatch, tmp_path):
    (tmp_path / "elsewhere").mkdir()
    computer = FakeComputer(monkeypatch, tmp_path / "elsewhere")
    project = tmp_path / "project"
    project.mkdir()
    (project / "pdt.yml").write_text("platform:\n  provider: aws\n")
    (project / ".env").write_text("ROOT_TOKEN=root-value\n")
    monkeypatch.setenv("PDT_PROJECT", str(project))
    coding_agents.ask("Why?", {"error text": "root-value"})
    assert computer.runs[0][1].endswith("<secret removed>")


def test_cursor_lists_its_models_without_the_env_file_values(monkeypatch, tmp_path):
    computer = FakeComputer(monkeypatch, tmp_path, programs=("cursor-agent",))
    envs = []

    def run(command, **kwargs):
        envs.append(kwargs["env"])
        return computer.run(command, **kwargs)

    monkeypatch.setattr(coding_agents.subprocess, "run", run)
    (tmp_path / ".env").write_text("API_TOKEN=file-value\n")
    monkeypatch.setenv("API_TOKEN", "file-value")
    coding_agents.ask("Why?")
    assert len(envs) == 2 and all("API_TOKEN" not in env for env in envs)


def test_on_windows_an_env_file_name_in_lower_case_is_kept_from_the_agent(monkeypatch, tmp_path):
    computer = FakeComputer(monkeypatch, tmp_path)
    monkeypatch.setattr(coding_agents, "ENV_NAME", str.upper)
    (tmp_path / ".env").write_text("api_token=file-value\n")
    monkeypatch.setenv("API_TOKEN", "terminal-value")
    coding_agents.ask("Why?")
    assert "API_TOKEN" not in computer.envs[0]
