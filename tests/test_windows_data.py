from pathlib import Path

from pdt import config


def test_machine_data_home_is_pdt_under_program_data(monkeypatch):
    monkeypatch.setenv("ProgramData", r"D:\ProgramData")
    assert config.machine_data_home() == Path(r"D:\ProgramData") / "pdt"


def test_machine_data_home_falls_back_to_the_system_drive(monkeypatch):
    monkeypatch.delenv("ProgramData", raising=False)
    assert config.machine_data_home() == Path(r"C:\ProgramData") / "pdt"
