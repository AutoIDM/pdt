"""The generated Azure shim forwards app output through Python's logging
library, because Azure Functions only ties a line to its run that way."""

import logging
import sys
import types

import pytest

from pdt.deploy_azure_functions import FUNCTION_APP


class Capture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        self.records.append(record)


@pytest.fixture
def shim(tmp_path, monkeypatch):
    monkeypatch.setenv("PDT_PROJECT", str(tmp_path))
    functions = types.SimpleNamespace(
        FunctionApp=lambda: types.SimpleNamespace(
            timer_trigger=lambda **kw: (lambda f: f)),
        TimerRequest=object)
    monkeypatch.setitem(sys.modules, "azure", types.SimpleNamespace(functions=functions))
    monkeypatch.setitem(sys.modules, "azure.functions", functions)
    saved_path = sys.path[:]
    namespace = {"__file__": str(tmp_path / "function_app.py")}
    exec(compile(FUNCTION_APP.format(app_name="my-app"), "function_app.py", "exec"),
         namespace)
    sys.path[:] = saved_path
    return namespace


@pytest.fixture
def captured():
    capture = Capture()
    logging.getLogger("pdt.app").addHandler(capture)
    yield capture
    logging.getLogger("pdt.app").removeHandler(capture)


def test_the_template_formats_and_compiles(shim):
    assert "run" in shim


def test_a_json_line_keeps_its_severity(shim, captured):
    stream = shim["LogStream"](logging.INFO)
    stream.write('{"severity": "ERROR", "message": "boom", "app": "x"}\n')
    [record] = captured.records
    assert record.levelno == logging.ERROR
    assert record.getMessage() == "boom  app=x"


def test_a_plain_line_arrives_at_the_stream_level(shim, captured):
    stream = shim["LogStream"](logging.INFO)
    stream.write("partial")
    stream.write(" line\nnext")
    stream.flush()
    assert [r.getMessage() for r in captured.records] == ["partial line", "next"]
    assert [r.levelno for r in captured.records] == [logging.INFO, logging.INFO]


def test_stderr_arrives_as_error(shim, captured):
    shim["LogStream"](logging.ERROR).write("Traceback (most recent call last):\n")
    [record] = captured.records
    assert record.levelno == logging.ERROR


def test_blank_lines_are_dropped(shim, captured):
    stream = shim["LogStream"](logging.INFO)
    stream.write("\n  \n")
    stream.flush()
    assert captured.records == []
