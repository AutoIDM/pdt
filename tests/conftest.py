import sys
from pathlib import Path

import pytest
from rich.text import Text

from pdt import console

# verify/scripts holds the live verification framework. It ships with the
# user's project, not with the wheel, so it is not importable as a package.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "verify" / "scripts"))


@pytest.fixture(autouse=True)
def console_on_stdout(monkeypatch):
    # A --json command moves pdt.console to stderr for the rest of the process.
    monkeypatch.setattr(console._console, "stderr", False)


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.delenv("PDT_PROJECT", raising=False)
    (tmp_path / "pdt.yml").write_text("platform:\n  provider: azure\n")
    monkeypatch.chdir(tmp_path)
    return tmp_path


def plain(lines):
    """Each marked-up line as a terminal shows it, with no styling."""
    return [Text.from_markup(line).plain for line in lines]


def add_app(root, name, config_text="", run_body="def main():\n    return 0\n"):
    folder = root / name
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "run.py").write_text(run_body)
    if config_text:
        (folder / "config.yml").write_text(config_text)
    return folder
