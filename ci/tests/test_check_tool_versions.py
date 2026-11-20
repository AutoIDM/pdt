"""check_tool_versions.py: which pins warn and which fail."""

import importlib.util
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "check_tool_versions.py"

spec = importlib.util.spec_from_file_location("check_tool_versions", SCRIPT)
tools = importlib.util.module_from_spec(spec)
sys.modules["check_tool_versions"] = tools
spec.loader.exec_module(tools)

OSV = ("npm", "apache-arrow")


def osv_answer(monkeypatch, answer):
    monkeypatch.setattr(tools, "read_json", lambda url, data=None: answer)


def test_every_pin_is_read_from_its_source_file():
    for _, source, constant, _, _ in tools.TOOLS:
        assert tools.pinned_version(tools.REPO / source, constant)


def test_the_newest_release_prints_no_annotation(monkeypatch, capsys):
    osv_answer(monkeypatch, {})
    assert tools.check("Apache Arrow", "src/pdt/duckdb_wasm.py", "ARROW_VERSION",
                       lambda: "17.0.0", OSV)
    assert capsys.readouterr().out == "Apache Arrow 17.0.0: newest release\n"


def test_a_newer_release_only_warns(monkeypatch, capsys):
    osv_answer(monkeypatch, {})
    assert tools.check("Apache Arrow", "src/pdt/duckdb_wasm.py", "ARROW_VERSION",
                       lambda: "21.2.0", OSV)
    assert capsys.readouterr().out.startswith("::warning file=src/pdt/duckdb_wasm.py::")


def test_a_known_vulnerability_fails_with_the_fixed_version(monkeypatch, capsys):
    osv_answer(monkeypatch, {"vulns": [{"id": "GHSA-xxxx", "affected": [
        {"package": {"name": "other"}, "ranges": [{"events": [{"fixed": "1.0"}]}]},
        {"package": {"name": "apache-arrow"},
         "ranges": [{"events": [{"introduced": "0"}, {"fixed": "18.0.0"}]}]}]}]})
    assert not tools.check("Apache Arrow", "src/pdt/duckdb_wasm.py", "ARROW_VERSION",
                           lambda: "17.0.0", OSV)
    assert ("::error file=src/pdt/duckdb_wasm.py::Apache Arrow 17.0.0: GHSA-xxxx, "
            "fixed in 18.0.0") in capsys.readouterr().out


def test_a_tool_without_osv_data_gets_only_the_release_check(monkeypatch, capsys):
    monkeypatch.setattr(tools, "read_json", None)
    assert tools.check("Google Cloud CLI", "src/pdt/gcloud_sdk.py", "VERSION",
                       lambda: "1.0.0", None)
