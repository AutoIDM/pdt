import pytest

from pdt import completion


@pytest.fixture(autouse=True)
def no_completion_offer(monkeypatch, tmp_path):
    """Commands offer tab completion after they finish; tests opt in explicitly."""
    monkeypatch.setattr(completion, "can_ask", lambda: False)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "data"))


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.delenv("PDT_PROJECT", raising=False)
    (tmp_path / "pdt.yml").write_text("platform:\n  provider: azure\n")
    monkeypatch.chdir(tmp_path)
    return tmp_path


def add_app(root, name, config_text="", run_body="def main():\n    return 0\n"):
    folder = root / name
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "run.py").write_text(run_body)
    if config_text:
        (folder / "config.yml").write_text(config_text)
    return folder
