import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from rich.text import Text

from pdt import console, deploy_common

from pdt.migrations import MIGRATIONS

# verify/scripts holds the live verification framework. It ships with the
# user's project, not with the wheel, so it is not importable as a package.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "verify" / "scripts"))


@pytest.fixture(autouse=True)
def console_on_stdout(monkeypatch):
    # A --json command moves pdt.console to stderr for the rest of the process.
    monkeypatch.setattr(console._console, "stderr", False)
    monkeypatch.setattr(console, "_warned", set())


@pytest.fixture(autouse=True)
def price_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(deploy_common, "PRICE_CACHE", tmp_path / "prices")
    return tmp_path / "prices"


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.delenv("PDT_PROJECT", raising=False)
    (tmp_path / "pdt.yml").write_text(
        f"migrated_by: {MIGRATIONS[-1].release}\nplatform:\n  provider: azure\n")
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


def started(run):
    """A subprocess.Popen for lifecycle.run_job that calls `run` as if it were subprocess.run."""
    return lambda command, **kwargs: SimpleNamespace(
        wait=lambda: run(command, **kwargs).returncode)
