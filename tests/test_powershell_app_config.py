from pathlib import Path

from pdt import config


def ps_app(project, name="ad-report", scripts=("report.ps1",), config_text="schedule: daily\n"):
    folder = project / name
    folder.mkdir()
    for script in scripts:
        (folder / script).write_text("Write-Host hi\n")
    (folder / "config.yml").write_text(config_text)
    return folder


def test_a_folder_of_ps1_files_with_no_run_py_is_an_app(project):
    ps_app(project, scripts=("b.ps1", "a.ps1"))
    assert config.app_folders() == ["ad-report"]
    assert config.powershell_scripts(project / "ad-report") == ["a.ps1", "b.ps1"]
    assert config.merged_app("ad-report")["run_scripts"] is None


def test_a_folder_with_run_py_is_a_python_app_even_with_ps1_files(project):
    folder = ps_app(project)
    (folder / "run.py").write_text("")
    assert config.powershell_scripts(folder) == []
    assert not config.uses_email(config.merged_app("ad-report"))


def test_requirements_psd1_next_to_run_py_keeps_a_powershell_app(project):
    folder = ps_app(project)
    (folder / "run.py").write_text("")
    (folder / "requirements.psd1").write_text("@{}\n")
    assert config.powershell_scripts(folder) == ["report.ps1"]
    assert config.app_folders() == ["ad-report"]


def test_an_empty_folder_is_not_an_app(project):
    (project / "notes").mkdir()
    assert config.app_folders() == []
    assert not config.is_app(Path(project / "notes"))


def test_run_scripts_must_name_ps1_files_of_the_app(project):
    ps_app(project, scripts=("a.ps1", "b.ps1"),
           config_text="schedule: daily\nrun_scripts: [b.ps1, c.ps1]\n")
    assert config.validate_app("ad-report") == [
        "ad-report/config.yml: run_scripts names 'c.ps1', which is not a .ps1 file in ad-report/"]
    (project / "ad-report" / "config.yml").write_text("schedule: daily\nrun_scripts: [b.ps1, a.ps1]\n")
    assert config.validate_app("ad-report") == []
    assert config.merged_app("ad-report")["run_scripts"] == ["b.ps1", "a.ps1"]


def test_run_scripts_is_refused_on_a_python_app(project):
    folder = ps_app(project, config_text="schedule: daily\nrun_scripts: [report.ps1]\n")
    (folder / "run.py").write_text("")
    assert config.validate_app("ad-report") == [
        "ad-report/config.yml: run_scripts only applies to an app made of .ps1 files; remove the key"]


def test_run_scripts_must_be_a_list(project):
    ps_app(project, config_text="schedule: daily\nrun_scripts: report.ps1\n")
    assert config.validate_app("ad-report") == [
        "ad-report/config.yml: run_scripts must be a list of .ps1 file names in the order to run them"]
    for value in ("[]", "[1]"):
        (project / "ad-report" / "config.yml").write_text(f"schedule: daily\nrun_scripts: {value}\n")
        assert config.validate_app("ad-report") == [
            "ad-report/config.yml: run_scripts must be a list of .ps1 file names in the order to run them"]


def test_ps1_files_match_in_any_case(project):
    ps_app(project, scripts=("Report.PS1",))
    assert config.powershell_scripts(project / "ad-report") == ["Report.PS1"]
