import hashlib
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from conftest import add_app
from pdt import cli, config, migrations, scaffold
from pdt.deploy_common import DOCKERFILE

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "projects"
RELEASES = sorted(path.name for path in FIXTURES.iterdir())
NEWEST = migrations.MIGRATIONS[-1].release


def copy_fixture(release, tmp_path, monkeypatch):
    project = tmp_path / "project"
    shutil.copytree(FIXTURES / release, project)
    monkeypatch.delenv("PDT_PROJECT", raising=False)
    monkeypatch.chdir(project)
    return project


def files(root):
    return {path.relative_to(root).as_posix(): path.read_bytes() for path in root.rglob("*")
            if path.is_file() and ".pdt" not in path.relative_to(root).parts
            and path.suffix != ".lock"}


def run_cli(monkeypatch, *argv):
    monkeypatch.setattr("sys.argv", ["pdt", *argv])
    return cli.main()


def test_the_fixtures_cover_every_release_on_pypi():
    assert RELEASES == ["0.1.0", "0.1.1", "0.1.2", "0.1.3", "0.1.4", "0.1.6"]


@pytest.mark.parametrize("release", RELEASES)
def test_each_release_reaches_the_current_shape(release, tmp_path, monkeypatch):
    project = copy_fixture(release, tmp_path, monkeypatch)
    migrations.bring_up_to_date(project)
    assert migrations.migrated_by(project) == NEWEST
    assert (project / "AGENTS.md").read_text() == scaffold.AGENTS_TEXT
    assert (project / "CLAUDE.md").read_text() == scaffold.CLAUDE_TEXT
    ignored = (project / ".gitignore").read_text().splitlines()
    assert set(scaffold.GITIGNORE_TEXT.splitlines()) <= set(ignored)
    assert [problem for problem in config.validate()
            if "migrated_by" in problem or "runtime" in problem] == []


@pytest.mark.parametrize("release", RELEASES)
def test_a_second_run_changes_nothing(release, tmp_path, monkeypatch, capsys):
    project = copy_fixture(release, tmp_path, monkeypatch)
    migrations.bring_up_to_date(project)
    capsys.readouterr()
    after = files(project)
    migrations.bring_up_to_date(project)
    assert files(project) == after
    assert capsys.readouterr().out == ""
    assert migrations.pending(project) == []
    for migration in migrations.MIGRATIONS:
        assert migration.plan(project) == migrations.Plan()


def test_a_project_with_every_change_only_gains_migrated_by(tmp_path, monkeypatch, capsys):
    project = copy_fixture("0.1.6", tmp_path, monkeypatch)
    (project / "AGENTS.md").write_text(scaffold.AGENTS_TEXT)
    before = files(project)
    migrations.bring_up_to_date(project)
    after = files(project)
    assert {name for name in after if after[name] != before.get(name)} == {"pdt.yml"}
    assert not (project / migrations.BACKUPS).exists()
    assert capsys.readouterr().out == ""


def test_the_output_names_each_file_and_the_backup(tmp_path, monkeypatch, capsys):
    project = copy_fixture("0.1.4", tmp_path, monkeypatch)
    migrations.bring_up_to_date(project)
    out = capsys.readouterr().out
    backup = next((project / migrations.BACKUPS).iterdir())
    assert out == (f"pdt {migrations.__version__} updated this project:\n"
                   f"  AGENTS.md {migrations.AGENTS_TITLE}\n"
                   f"  The files as they were are in .pdt/backups/{backup.name}/.\n\n")
    assert (backup / "AGENTS.md").read_bytes() == (FIXTURES / "0.1.4" / "AGENTS.md").read_bytes()
    assert (backup / "pdt.yml").read_bytes() == (FIXTURES / "0.1.4" / "pdt.yml").read_bytes()
    assert (project / ".pdt" / ".gitignore").read_text() == "*\n"


def old_project(tmp_path, monkeypatch):
    project = copy_fixture("0.1.0", tmp_path, monkeypatch)
    (project / "pdt.yml").write_text(
        "platform:\n  provider: aws\n  runtime: fargate\n  region: us-east-1\napps: []\n")
    (project / "hello-world" / "Dockerfile").write_text(
        'FROM python\nENTRYPOINT ["uv", "run", "--script", "run.py"]\n')
    (project / ".env").write_text("PDT_AZURE_CONTAINER_APPS_ENVIRONMENT=jobs\n")
    return project


def test_a_crash_after_any_write_finishes_on_the_next_run(tmp_path, monkeypatch, capsys):
    clean = old_project(tmp_path / "clean", monkeypatch)
    writes = []
    original = config.write_text_atomically
    monkeypatch.setattr(config, "write_text_atomically",
                        lambda path, text: (writes.append(path), original(path, text)))
    migrations.bring_up_to_date(clean)
    expected = files(clean)
    assert len(writes) > 5
    for crash_after in range(len(writes)):
        project = old_project(tmp_path / str(crash_after), monkeypatch)
        count = []

        def write_then_crash(path, text):
            if len(count) == crash_after:
                raise OSError("disk full")
            count.append(path)
            original(path, text)

        monkeypatch.setattr(config, "write_text_atomically", write_then_crash)
        with pytest.raises(OSError):
            migrations.bring_up_to_date(project)
        if crash_after > 0:
            assert "Run the command again to finish" in capsys.readouterr().out
        monkeypatch.setattr(config, "write_text_atomically", original)
        migrations.bring_up_to_date(project)
        assert files(project) == expected


def test_user_text_survives_around_the_removed_key(project):
    text = ("# my jobs\nplatform:\n    provider: 'aws'   # keep\n    runtime: fargate  # old\n"
            "    region: \"us-east-1\"\n\napps:\n- name: report\n  platform:\n"
            "     runtime: fargate\n     region: eu-west-1  # here\n")
    (project / "pdt.yml").write_text(text)
    add_app(project, "report", "schedule: daily\nplatform:\n  runtime: fargate\nconfig:\n  runtime: 30\n")
    migrations.bring_up_to_date(project)
    assert (project / "pdt.yml").read_text() == (
        f"# {migrations.MIGRATED_BY_COMMENT}\nmigrated_by: {NEWEST}\n\n# my jobs\n"
        "platform:\n    provider: 'aws'   # keep\n"
        "    region: \"us-east-1\"\n\napps:\n- name: report\n  platform:\n"
        "     region: eu-west-1  # here\n")
    assert (project / "report" / "config.yml").read_text() == (
        "schedule: daily\nplatform:\nconfig:\n  runtime: 30\n")
    assert config.merged_app("report")["config"] == {"runtime": 30}
    assert config.validate() == []


def test_an_old_runtime_stops_a_command_that_changes_things(project, capsys):
    (project / "pdt.yml").write_text("platform:\n  provider: aws\n")
    add_app(project, "report", "platform:\n  runtime: lambda\n")
    before = files(project)
    with pytest.raises(migrations.Blocked) as stopped:
        migrations.start("deploy")
    message = str(stopped.value)
    assert "uvx --from pdt-cli==0.1.0 pdt destroy report" in message
    assert "2. Delete the line `runtime: lambda` from report/config.yml." in message
    assert "3. Run your pdt command again." in message
    assert files(project) == before


def test_an_old_runtime_lets_a_command_that_reads_go_on(project, monkeypatch, capsys):
    (project / "pdt.yml").write_text("platform:\n  provider: aws\n")
    add_app(project, "report", "platform:\n  runtime: functions\n")
    assert run_cli(monkeypatch, "list") == 0
    out = capsys.readouterr().out
    assert "note: pdt" in out
    assert "Azure Function App" in out
    assert "report" in out.splitlines()[-1]
    assert migrations.migrated_by(project) is None


def test_azure_environment_gets_its_resource_group(project):
    add_app(project, "report")
    (project / ".env").write_text("A=1\nPDT_AZURE_CONTAINER_APPS_ENVIRONMENT=jobs  # shared\n")
    (project / "report" / ".env").write_text(
        "PDT_AZURE_RESOURCE_GROUP=ops\nexport PDT_AZURE_CONTAINER_APPS_ENVIRONMENT=\"env\"")
    assert migrations.azure_environment(project).writes == {
        ".env": "A=1\nPDT_AZURE_CONTAINER_APPS_ENVIRONMENT=pdt/jobs  # shared\n",
        "report/.env": "PDT_AZURE_RESOURCE_GROUP=ops\nexport PDT_AZURE_CONTAINER_APPS_ENVIRONMENT=\"ops/env\"",
    }


def test_a_backup_of_env_is_private(project):
    (project / "pdt.yml").write_text("platform:\n  provider: azure\n")
    (project / ".env").write_text("PDT_AZURE_CONTAINER_APPS_ENVIRONMENT=jobs\n")
    migrations.bring_up_to_date(project)
    backup = next((project / migrations.BACKUPS).iterdir())
    assert (backup / ".env").read_text() == "PDT_AZURE_CONTAINER_APPS_ENVIRONMENT=jobs\n"
    if os.name != "nt":
        assert (backup / ".env").stat().st_mode & 0o777 == 0o600


def test_the_old_dockerfile_entrypoint_reports_the_exit_code(project, capsys):
    (project / "pdt.yml").write_text("platform:\n  provider: azure\n")
    add_app(project, "report")
    add_app(project, "custom")
    (project / "report" / "Dockerfile").write_text(
        "FROM python\nENTRYPOINT [\"uv\", \"run\",  \"--script\", \"run.py\"]\n")
    (project / "custom" / "Dockerfile").write_text("FROM python\nENTRYPOINT [\"python\", \"run.py\"]\n")
    migrations.bring_up_to_date(project)
    entrypoint = next(line for line in DOCKERFILE.splitlines() if line.startswith("ENTRYPOINT"))
    assert (project / "report" / "Dockerfile").read_text() == f"FROM python\n{entrypoint}\n"
    assert (project / "custom" / "Dockerfile").read_text() == "FROM python\nENTRYPOINT [\"python\", \"run.py\"]\n"
    out = capsys.readouterr().out
    assert "Next steps:\n" in out
    assert "\n  pdt deploy report    the deployed job uses the old Dockerfile" in out
    assert "custom/Dockerfile must print `pdt: exit <code>`" in out


def test_gitignore_keeps_the_user_lines(project):
    (project / ".gitignore").write_text("node_modules/\n.env")
    assert migrations.gitignore(project).writes[".gitignore"] == (
        "node_modules/\n.env\n"
        + "".join(f"{line}\n" for line in scaffold.GITIGNORE_TEXT.splitlines() if line != ".env"))


def test_an_edited_agents_file_stays_and_its_new_text_is_in_the_backup(project, capsys):
    (project / "pdt.yml").write_text("platform:\n  provider: azure\n")
    (project / "AGENTS.md").write_text("mine\n")
    migrations.bring_up_to_date(project)
    assert (project / "AGENTS.md").read_text() == "mine\n"
    backup = next((project / migrations.BACKUPS).iterdir())
    assert (backup / "AGENTS.md.new").read_text() == scaffold.AGENTS_TEXT
    out = capsys.readouterr().out
    assert out.count("AGENTS.md has your own edits") == 1
    assert f".pdt/backups/{backup.name}/AGENTS.md.new" in out


def test_a_shipped_agents_file_is_replaced(project, monkeypatch):
    old = "# AGENTS.md\n\nThis folder is a pdt project.\n"
    monkeypatch.setitem(migrations.SHIPPED["AGENTS.md"], hashlib.sha256(old.encode()).hexdigest(),
                        "0.1.1")
    (project / "AGENTS.md").write_text(old)
    assert migrations.agents_file(project).writes == {"AGENTS.md": scaffold.AGENTS_TEXT}


def test_a_newer_project_stops_every_command(project, monkeypatch, capsys):
    (project / "pdt.yml").write_text("migrated_by: 9.0.0\nplatform:\n  provider: azure\n")
    add_app(project, "report")
    before = files(project)
    assert run_cli(monkeypatch, "list") == 1
    out = capsys.readouterr().out
    assert "error: this project needs pdt 9.0.0 or newer." in out
    assert "uv tool upgrade pdt-cli" in out
    assert files(project) == before


def test_the_library_ignores_migrated_by(project):
    (project / "pdt.yml").write_text("migrated_by: 9.0.0\nplatform:\n  provider: azure\n")
    add_app(project, "report", "schedule: daily\n")
    assert config.merged_app("report")["platform"] == {"provider": "azure"}
    assert config.validate() == []


def test_migrated_by_in_an_app_file_belongs_in_the_project_file(project):
    add_app(project, "report", "migrated_by: 0.1.6\n")
    assert config.validate() == [
        "report/config.yml: 'migrated_by' belongs in the top level of pdt.yml, not here"]


def test_a_project_that_init_writes_is_up_to_date():
    text = scaffold.project_yaml({"provider": "aws", "region": "us-east-1"})
    assert yaml.safe_load(text)["migrated_by"] == NEWEST


def test_dry_run_prints_a_diff_and_changes_nothing(tmp_path, monkeypatch, capsys):
    project = copy_fixture("0.1.4", tmp_path, monkeypatch)
    before = files(project)
    assert run_cli(monkeypatch, "migrate", "--dry-run") == 1
    out = capsys.readouterr().out
    assert "--- a/AGENTS.md\n+++ b/AGENTS.md\n" in out
    assert f"+migrated_by: {NEWEST}\n" in out
    assert files(project) == before
    assert not (project / ".pdt").exists()
    assert run_cli(monkeypatch, "migrate") == 0
    capsys.readouterr()
    assert run_cli(monkeypatch, "migrate", "--dry-run") == 0
    assert capsys.readouterr().out == f"This project is up to date for pdt {migrations.__version__}.\n"


def test_json_output_keeps_the_migration_lines_out_of_stdout(tmp_path, monkeypatch, capsys):
    copy_fixture("0.1.4", tmp_path, monkeypatch)
    migrations.start("runs", json_output=True)
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "AGENTS.md" in captured.err


def test_only_the_newest_backups_stay(project, monkeypatch):
    for day in range(1, 8):
        stamp = datetime(2026, 10, day, tzinfo=timezone.utc)
        monkeypatch.setattr(migrations, "datetime", SimpleNamespace(now=lambda tz, stamp=stamp: stamp))
        (project / "pdt.yml").write_text("platform:\n  provider: azure\n")
        (project / "AGENTS.md").unlink(missing_ok=True)
        migrations.bring_up_to_date(project)
    names = sorted(path.name for path in (project / migrations.BACKUPS).iterdir())
    assert names == [f"2026-10-0{day}T00-00-00Z" for day in range(3, 8)]


def test_yaml_edits_leave_shared_lines_alone():
    assert config.remove_key("platform: {provider: aws, runtime: x}\n", ("platform", "runtime")) is None
    assert config.remove_key("apps:\n- runtime: x\n  name: a\n", ("apps", 0, "runtime")) is None
    assert config.remove_key("a: 1\n", ("b",)) == "a: 1\n"
    assert config.remove_key("platform:\n  runtime:\n  region: x\n", ("platform", "runtime")) == (
        "platform:\n  region: x\n")
    assert config.rename_key("platform:\n  zone: x  # z\n", ("platform", "zone"), "region") == (
        "platform:\n  region: x  # z\n")
    assert config.set_top_level_key("migrated_by: 0.1.1  # c\napps: []\n", "migrated_by", "0.1.6") == (
        "migrated_by: 0.1.6  # c\napps: []\n")
    assert config.set_top_level_key("migrated_by:\n", "migrated_by", "0.1.6") == "migrated_by: 0.1.6\n"
    assert config.set_top_level_key("", "migrated_by", "0.1.6") == "migrated_by: 0.1.6\n"


def test_the_registry_runs_in_release_order():
    releases = [migrations.version(migration.release) for migration in migrations.MIGRATIONS]
    assert releases == sorted(releases)


def test_shipped_holds_the_current_generated_texts():
    current = hashlib.sha256(scaffold.AGENTS_TEXT.encode()).hexdigest()
    assert current in migrations.SHIPPED["AGENTS.md"], (
        f"AGENTS_TEXT in scaffold.py changed, and its hash {current} is not in "
        "migrations.SHIPPED['AGENTS.md']. Add it with the release that ships the new text, and "
        "add Migration(<that release>, AGENTS_TITLE, agents_file) to the end of MIGRATIONS. "
        "When two open PRs change AGENTS_TEXT, the PR that merges second does this.")
    assert hashlib.sha256(scaffold.CLAUDE_TEXT.encode()).hexdigest() in migrations.SHIPPED["CLAUDE.md"]
    agents_releases = [migration.release for migration in migrations.MIGRATIONS
                       if migration.plan is migrations.agents_file]
    assert agents_releases == sorted(set(migrations.SHIPPED["AGENTS.md"].values()),
                                     key=migrations.version)


@pytest.mark.parametrize("release", ["0.1.1", "0.1.3", "0.1.4", "0.1.6"])
def test_shipped_matches_the_fixture_of_each_release(release):
    text = (FIXTURES / release / "AGENTS.md").read_text()
    assert migrations.SHIPPED["AGENTS.md"][hashlib.sha256(text.encode()).hexdigest()] <= release
