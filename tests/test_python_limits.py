import re
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

from pdt import cli

ROOT = Path(__file__).resolve().parent.parent
PACKAGE = ROOT / "src" / "pdt"
HEADER_LIMIT = re.compile(r'^# requires-python = "([^"]+)"$', re.M)
PACKAGE_LOAD = re.compile(r'^if "pdt" not in sys\.modules:\n(?:    .*\n)+', re.M)


def package_limit() -> str:
    return tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["requires-python"]


def internal_scripts() -> list[Path]:
    return [path for path in PACKAGE.glob("*.py") if path.read_text().startswith("#!/usr/bin/env -S uv run --script")]


def test_every_internal_script_has_the_python_limit_of_the_package():
    scripts = internal_scripts()
    assert len(scripts) >= 8
    for script in scripts:
        assert HEADER_LIMIT.findall(script.read_text()) == [package_limit()], script.name


def test_no_internal_script_puts_the_folder_above_it_on_the_import_path():
    for script in internal_scripts():
        text = script.read_text()
        assert "resolve().parent.parent))" not in text, script.name
        if "from pdt import" in text:
            assert PACKAGE_LOAD.search(text), script.name


def test_a_script_loads_only_pdt_from_the_folder_above_it(tmp_path):
    site = tmp_path / "site-packages"
    (site / "pdt").mkdir(parents=True)
    shutil.copy(PACKAGE / "__init__.py", site / "pdt" / "__init__.py")
    (site / "pdt" / "probe_module.py").write_text("NAME = 'probe'\n")
    (site / "tool_only_package").mkdir()
    (site / "tool_only_package" / "__init__.py").write_text("")
    load = PACKAGE_LOAD.search((PACKAGE / "deploy_azure.py").read_text()).group(0)
    (site / "pdt" / "probe.py").write_text(
        "import importlib.util\nimport sys\nfrom pathlib import Path\n\n" + load
        + "from pdt import probe_module\nprint(probe_module.NAME)\n"
        + "try:\n    import tool_only_package\n    print('tool package loaded')\n"
        + "except ModuleNotFoundError:\n    print('tool package not loaded')\n")
    done = subprocess.run([sys.executable, "-I", str(site / "pdt" / "probe.py")], capture_output=True, text=True)
    assert (done.returncode, done.stdout, done.stderr) == (0, "probe\ntool package not loaded\n", "")


def test_pdt_stops_on_a_python_above_its_limit(monkeypatch, capsys):
    monkeypatch.setattr("importlib.metadata.metadata", lambda name: {"Requires-Python": ">=3.12,<3.15"})
    monkeypatch.setattr(sys, "version_info", (3, 15, 0, "final", 0))
    monkeypatch.setattr(sys, "argv", ["pdt", "list"])
    assert cli.run() == 1
    text = " ".join(capsys.readouterr().out.split())
    assert "pdt runs on Python 3.12 to 3.14, but this pdt runs on Python 3.15." in text
    assert "uv tool install --force --python 3.12 pdt-cli" in text


def test_pdt_starts_on_a_python_inside_its_limit(monkeypatch):
    monkeypatch.setattr("importlib.metadata.metadata", lambda name: {"Requires-Python": ">=3.12,<3.15"})
    monkeypatch.setattr(sys, "version_info", (3, 14, 7, "final", 0))
    assert cli.unsupported_python() == ""
