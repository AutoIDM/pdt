import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from rich.text import Text

from pdt import console, deploy_common, watch

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


@pytest.fixture(autouse=True)
def history(tmp_path, monkeypatch):
    monkeypatch.setattr(watch, "HISTORY", tmp_path / "history.jsonl")
    monkeypatch.setattr(watch, "_seen", ("", ""))
    for name in ("CI", watch.NOTE_ENV, watch.OFF_ENV, *watch.AGENT_ENVS):
        monkeypatch.delenv(name, raising=False)
    return tmp_path / "history.jsonl"


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.delenv("PDT_PROJECT", raising=False)
    (tmp_path / "pdt.yml").write_text("platform:\n  provider: azure\n")
    monkeypatch.chdir(tmp_path)
    return tmp_path


def plain(lines):
    """Each marked-up line as a terminal shows it, with no styling."""
    return [Text.from_markup(line).plain for line in lines]


def fake_azure_reads(monkeypatch, read):
    from pdt import deploy_azure, deploy_azure_container_apps

    def get(token, url, az_command, missing=(404,)):
        return read(*az_command)

    for module in (deploy_azure, deploy_azure_container_apps):
        monkeypatch.setattr(module, "arm_get", get)
        monkeypatch.setattr(module, "arm_list", get)
        monkeypatch.setattr(module, "access_token", lambda resource="": "token")
    monkeypatch.setattr(deploy_azure_container_apps, "price_list",
                        lambda region, currency, secrets, storage: (
                            {"vCPU": 1.0, "memory": 1.0, "registry": 1.0, "Key Vault": 1.0,
                             "storage": 1.0}, "USD", ""))


def add_app(root, name, config_text="", run_body="def main():\n    return 0\n"):
    folder = root / name
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "run.py").write_text(run_body)
    if config_text:
        (folder / "config.yml").write_text(config_text)
    return folder


def built_image(app, image, image_platform, cloud_builder, deployed, current, build):
    """deploy_common.ensure_image for a deploy test with no app folder: it always builds."""
    build(Path("stage"))
    return "f" * 32


def started(run):
    """A subprocess.Popen for lifecycle.run_job that calls `run` as if it were subprocess.run."""
    return lambda command, **kwargs: SimpleNamespace(
        wait=lambda: run(command, **kwargs).returncode)
