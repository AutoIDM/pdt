import hashlib

from conftest import add_app
from pdt import cli, config, migrate, scaffold
from pdt.deploy_common import DOCKERFILE

CURRENT_AGENTS = "1c10215b385db9231333330af082dbb27da8fdc4949f35e3f33352785845e745"


def up_to_date(project):
    for name, text in ((".gitignore", scaffold.GITIGNORE_TEXT), ("AGENTS.md", scaffold.AGENTS_TEXT),
                       ("CLAUDE.md", scaffold.CLAUDE_TEXT)):
        (project / name).write_text(text)


def test_an_up_to_date_project_needs_nothing(project, capsys):
    up_to_date(project)
    assert migrate.findings() == []
    assert migrate.run(assume_yes=True) == 0
    assert "up to date" in capsys.readouterr().out


def test_runtime_leaves_platform_but_not_config(project):
    up_to_date(project)
    (project / "pdt.yml").write_text(
        "platform:\n  provider: aws  # where jobs run\n  runtime: fargate\n  region: us-east-1\n"
        "apps:\n  - name: report\n    platform:\n      runtime: fargate\n")
    add_app(project, "report", "schedule: daily\nplatform:\n  runtime: fargate\n"
                               "config:\n  runtime: 30\n")
    assert migrate.run(assume_yes=True) == 0
    assert (project / "pdt.yml").read_text() == (
        "platform:\n  provider: aws  # where jobs run\n  region: us-east-1\n"
        "apps:\n  - name: report\n    platform:\n")
    assert (project / "report" / "config.yml").read_text() == (
        "schedule: daily\nplatform:\nconfig:\n  runtime: 30\n")
    assert config.merged_app("report")["config"] == {"runtime": 30}
    assert config.validate() == []
    assert migrate.findings() == []


def test_a_lambda_runtime_tells_the_user_to_remove_the_old_function(project, capsys):
    up_to_date(project)
    add_app(project, "report", "platform:\n  provider: aws\n  runtime: lambda\n")
    assert migrate.run(assume_yes=True) == 1
    out = capsys.readouterr().out
    assert "AWS Lambda function" in out
    assert "uvx --from pdt-cli==0.1.0 pdt destroy" in out
    assert "runtime" not in (project / "report" / "config.yml").read_text()


def test_azure_environment_gets_its_resource_group(project):
    up_to_date(project)
    add_app(project, "report")
    (project / ".env").write_text("A=1\nPDT_AZURE_CONTAINER_APPS_ENVIRONMENT=jobs  # shared\n")
    (project / "report" / ".env").write_text(
        "PDT_AZURE_RESOURCE_GROUP=ops\nexport PDT_AZURE_CONTAINER_APPS_ENVIRONMENT=\"env\"\n")
    assert migrate.run(assume_yes=True) == 0
    assert (project / ".env").read_text() == (
        "A=1\nPDT_AZURE_CONTAINER_APPS_ENVIRONMENT=pdt/jobs  # shared\n")
    assert (project / "report" / ".env").read_text() == (
        "PDT_AZURE_RESOURCE_GROUP=ops\nexport PDT_AZURE_CONTAINER_APPS_ENVIRONMENT=\"ops/env\"\n")
    assert migrate.findings() == []


def test_the_old_dockerfile_entrypoint_reports_the_exit_code(project):
    up_to_date(project)
    add_app(project, "report")
    add_app(project, "custom")
    (project / "report" / "Dockerfile").write_text(
        "FROM python\nENTRYPOINT [\"uv\", \"run\",  \"--script\", \"run.py\"]\n")
    (project / "custom" / "Dockerfile").write_text("FROM python\nENTRYPOINT [\"python\", \"run.py\"]\n")
    found = migrate.findings()
    assert [(f.path.parent.name, f.apply is None) for f in found] == [
        ("custom", True), ("report", False)]
    assert migrate.run(assume_yes=True) == 1
    entrypoint = next(line for line in DOCKERFILE.splitlines() if line.startswith("ENTRYPOINT"))
    assert (project / "report" / "Dockerfile").read_text() == f"FROM python\n{entrypoint}\n"


def test_project_files_are_created_and_an_unedited_agents_file_is_updated(project, monkeypatch):
    old = "# AGENTS.md\n\nThis folder is a pdt project: a set of small scheduled jobs.\n"
    monkeypatch.setattr(migrate, "SHIPPED_AGENTS", {hashlib.sha256(old.encode()).hexdigest()})
    (project / "AGENTS.md").write_text(old)
    (project / ".gitignore").write_text("node_modules/\n.env")
    assert migrate.run(assume_yes=True) == 0
    assert (project / "AGENTS.md").read_text() == scaffold.AGENTS_TEXT
    assert (project / "CLAUDE.md").read_text() == scaffold.CLAUDE_TEXT
    ignored = (project / ".gitignore").read_text().splitlines()
    assert ignored[:2] == ["node_modules/", ".env"]
    assert set(scaffold.GITIGNORE_TEXT.splitlines()) <= set(ignored)
    assert ignored.count(".env") == 1


def test_an_edited_agents_file_is_left_alone(project):
    up_to_date(project)
    (project / "AGENTS.md").write_text("mine\n")
    assert migrate.findings() == []


def test_every_agents_text_pdt_shipped_is_known():
    current = hashlib.sha256(scaffold.AGENTS_TEXT.encode()).hexdigest()
    assert current == CURRENT_AGENTS, (
        "AGENTS_TEXT changed: add the old hash in CURRENT_AGENTS to migrate.SHIPPED_AGENTS, "
        "then set CURRENT_AGENTS to the new hash")
    assert current not in migrate.SHIPPED_AGENTS


def test_nothing_changes_without_an_answer(project, monkeypatch):
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    assert migrate.run(assume_yes=False) == 1
    assert not (project / "AGENTS.md").exists()


def test_validate_points_at_migrate(project, monkeypatch, capsys):
    up_to_date(project)
    add_app(project, "report", "platform:\n  runtime: fargate\n")
    monkeypatch.setattr("sys.argv", ["pdt", "validate"])
    assert cli.main() == 1
    out = capsys.readouterr().out
    assert "no longer a setting" in out
    assert "pdt migrate" in out
