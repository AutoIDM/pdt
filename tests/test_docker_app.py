"""A Docker app: a folder with a Dockerfile and neither run.py nor a .ps1 file."""

import json
import shlex
import subprocess
import sys

import pytest

from conftest import plain
from pdt import __version__, config, docker_app, lifecycle, run_docker, scaffold
from pdt.config import find_apps, validate_app
from pdt.deploy_common import BASE_IMAGE, stage_build_context, write_dockerfile


def command(text):
    return docker_app.final_stage(text).command


def test_the_command_is_the_exec_form_cmd():
    assert command('FROM node:22\nCMD ["node", "hello.js"]\n') == ["node", "hello.js"]


def test_a_shell_form_cmd_runs_through_the_shell():
    assert command("FROM node:22\nCMD node hello.js > /dev/null\n") == [
        "/bin/sh", "-c", "node hello.js > /dev/null"]


def test_a_shell_instruction_changes_the_shell_of_a_shell_form():
    text = 'FROM alpine\nSHELL ["/bin/bash", "-lc"]\nCMD ./run\n'
    assert command(text) == ["/bin/bash", "-lc", "./run"]


def test_an_exec_entrypoint_takes_the_cmd_as_its_arguments():
    stage = docker_app.final_stage('FROM go\nENTRYPOINT ["/app/report"]\nCMD ["--days", "7"]\n')
    assert (stage.command, stage.source) == (["/app/report", "--days", "7"], "ENTRYPOINT and CMD")


def test_a_shell_entrypoint_ignores_the_cmd():
    stage = docker_app.final_stage("FROM go\nENTRYPOINT /app/report\nCMD [\"--days\", \"7\"]\n")
    assert (stage.command, stage.source) == (["/bin/sh", "-c", "/app/report"], "ENTRYPOINT")


def test_only_the_last_stage_counts():
    text = ('FROM golang:1.23 AS build\nUSER builder\nCMD ["go", "test"]\n'
            "FROM debian:bookworm-slim\nCOPY --from=build /out/report /report\n")
    assert docker_app.final_stage(text) == docker_app.Stage([], "", "")


def test_comments_continuations_and_heredocs_do_not_confuse_the_reader():
    text = ("# syntax=docker/dockerfile:1\n"
            "FROM python:3.12-slim\n"
            "RUN <<EOF\n"
            "CMD this line is a script, not an instruction\n"
            "EOF\n"
            "USER app\n"
            "cmd [\"python\", \\\n"
            "# a comment inside the instruction\n"
            "     \"main.py\"]\n")
    stage = docker_app.final_stage(text)
    assert (stage.command, stage.user) == (["python", "main.py"], "app")


def test_the_escape_directive_changes_the_continuation_character():
    text = '# escape=`\nFROM mcr.microsoft.com/powershell\nCMD ["pwsh", `\n  "-File", "a.ps1"]\n'
    assert command(text) == ["pwsh", "-File", "a.ps1"]


@pytest.fixture
def node_app(project):
    folder = project / "hello"
    folder.mkdir()
    (folder / "config.yml").write_text("schedule: daily\n")
    (folder / "Dockerfile").write_text('FROM node:22-slim\nWORKDIR /app\nCOPY hello.js .\n'
                                       'CMD ["node", "hello.js"]\n')
    (folder / "hello.js").write_text('console.log("hi");\n')
    return folder


def test_a_folder_with_only_a_dockerfile_is_a_docker_app(node_app):
    assert config.docker_app(node_app)
    assert find_apps() == ["hello"]
    assert validate_app("hello") == []


def test_a_dockerfile_next_to_run_py_or_scripts_is_not_a_docker_app(node_app):
    (node_app / "run.py").write_text("")
    assert not config.docker_app(node_app)
    (node_app / "run.py").unlink()
    (node_app / "a.ps1").write_text("")
    assert not config.docker_app(node_app)


def test_a_docker_app_needs_a_command(node_app):
    (node_app / "Dockerfile").write_text("FROM node:22-slim\n")
    assert validate_app("hello") == [
        "hello/Dockerfile names no command for the job to run; add a CMD line at the end of it, "
        'for example CMD ["node", "index.js"]']


def test_a_docker_app_does_not_deploy_to_windows(node_app, project):
    (project / "pdt.yml").write_text("platform:\n  provider: windows\n")
    assert validate_app("hello") == [
        "hello: platform.provider is windows, which runs jobs on this PC without containers, so it "
        "cannot run the image hello/Dockerfile builds. Set platform.provider to aws, azure, or "
        "google-cloud in hello/config.yml."]


def test_the_summary_names_the_command_and_a_state_reader(node_app):
    app = config.merged_app("hello")
    assert plain(docker_app.summary_lines(app)) == [
        "runs node hello.js, the CMD of hello/Dockerfile"]
    (node_app / "hello.js").write_text("const dir = process.env.PDT_STATE_DIR;\n")
    assert plain(docker_app.summary_lines(app))[1] == (
        "keeps PDT_STATE_DIR between runs (named in hello.js)")


def test_the_config_file_does_not_count_as_a_state_reader(node_app):
    (node_app / "config.yml").write_text("schedule: daily\n# PDT_STATE_DIR\n")
    assert docker_app.state_reader(node_app) == ""


def staged(app):
    stage = stage_build_context(app)
    write_dockerfile(stage, app)
    return stage


def entrypoint(dockerfile):
    line = next(line for line in dockerfile.splitlines() if line.startswith("ENTRYPOINT "))
    return json.loads(line.removeprefix("ENTRYPOINT "))


def test_a_docker_app_builds_in_its_own_folder_with_pdt_beside_it(node_app):
    stage = staged(config.merged_app("hello"))
    assert sorted(path.name for path in stage.iterdir()) == [
        ".pdt-lifecycle", ".pdt-project", "Dockerfile", "config.yml", "hello.js"]
    assert sorted(path.relative_to(stage / ".pdt-project").as_posix()
                  for path in (stage / ".pdt-project").rglob("*")) == [
        "hello", "hello/Dockerfile", "hello/config.yml", "pdt.yml"]
    assert (stage / ".pdt-lifecycle" / "pdt" / "lifecycle.py").is_file()


def test_the_image_starts_the_dockerfile_command_inside_the_lifecycle(node_app):
    text = (staged(config.merged_app("hello")) / "Dockerfile").read_text()
    own = (node_app / "Dockerfile").read_text()
    assert text.startswith(own)
    assert f"COPY --from={BASE_IMAGE} /usr/local/bin/uv /opt/pdt-uv/uv\n" in text
    assert f'"pdt-cli[apps]=={__version__}"' in text
    assert "ENV PDT_PROJECT=/workspace PDT_APP=hello " in text
    assert "USER" not in text
    script = entrypoint(text)[2]
    assert ("/opt/pdt/bin/python /opt/pdt-lifecycle/pdt/lifecycle.py -- /opt/pdt/bin/python -m "
            "pdt.run_docker /workspace/hello -- node hello.js & pid=$!") in script
    assert 'echo "pdt: exit $1"' in script


def test_the_image_installs_pdt_as_root_and_gives_the_user_back(node_app):
    (node_app / "Dockerfile").write_text('FROM node:22-slim\nUSER node\nCMD ["node", "hello.js"]\n')
    text = (staged(config.merged_app("hello")) / "Dockerfile").read_text()
    tail = text.split("# Added by pdt", 1)[1]
    assert tail.index("USER root\n") < tail.index("RUN ") < tail.index("USER node\n")
    assert tail.index("USER node\n") < tail.index("ENTRYPOINT ")


def test_a_command_with_quotes_reaches_the_shell_whole(node_app):
    (node_app / "Dockerfile").write_text("FROM node:22-slim\nCMD echo \"it's \\\"here\\\"\"\n")
    (node_app / "hello.js").write_text("process.env.PDT_STATE_DIR\n")
    script = entrypoint((staged(config.merged_app("hello")) / "Dockerfile").read_text())[2]
    start = script.split(" -- ", 1)[1].split(" & pid=", 1)[0]
    assert shlex.split(start) == ["/opt/pdt/bin/python", "-m", "pdt.run_docker",
                                  "/workspace/hello", "--state", "--",
                                  "/bin/sh", "-c", 'echo "it\'s \\"here\\""']


def test_pdt_run_builds_the_dockerfile_as_it_is(node_app):
    stage = stage_build_context(config.merged_app("hello"))
    write_dockerfile(stage, config.merged_app("hello"), wrap=False)
    assert (stage / "Dockerfile").read_text() == (node_app / "Dockerfile").read_text()


def test_an_app_dockerignore_keeps_its_patterns_and_never_drops_pdt(node_app):
    (node_app / ".dockerignore").write_text(".*\nnode_modules\n")
    ignore = (staged(config.merged_app("hello")) / ".dockerignore").read_text()
    assert ignore == (".*\nnode_modules\n!.pdt-lifecycle\n!.pdt-lifecycle/**\n"
                      "!.pdt-project\n!.pdt-project/**\n")


def test_the_lifecycle_takes_the_app_name_from_pdt_app(node_app, monkeypatch, tmp_path):
    runs = []
    monkeypatch.setattr(lifecycle, "run_job", lambda app, provider, *rest, **kw: runs.append(
        (app, provider)) or 0)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PDT_APP", "hello")
    assert lifecycle.main(["--", "true"]) == 0
    assert runs == [("hello", "azure")]


def test_the_wrapper_reads_its_arguments():
    assert run_docker.parse(["/w/hello", "--", "node", "a.js"])[1:] == (False, "", ["node", "a.js"])
    assert run_docker.parse(["/w/hello", "--state", "--image", "pdt-hello:local", "--", "x"])[1:] == (
        True, "pdt-hello:local", ["x"])
    assert run_docker.parse(["/w/hello", "node"]) is None
    assert run_docker.parse(["/w/hello", "--"]) is None
    assert run_docker.parse(["/w/hello", "--image", "--", "x"]) is None


@pytest.fixture
def in_image(node_app, monkeypatch):
    monkeypatch.chdir(node_app)
    monkeypatch.delenv("PDT_STORAGE_URL", raising=False)
    (node_app / "config.yml").write_text("schedule: daily\nconfig:\n  greeting: hi\n")
    return node_app


def test_the_command_gets_the_config_and_its_output_is_stored(in_image):
    program = ("import json, os, pathlib; c = json.loads(os.environ['PDT_CONFIG_JSON']); "
               "pathlib.Path(os.environ['PDT_OUTPUT_DIR'], 'out.txt').write_text(c['greeting'])")
    assert run_docker.main([str(in_image), "--", sys.executable, "-c", program]) == 0
    (run,) = (in_image.parent / ".pdt" / "storage" / "hello" / "runs").iterdir()
    assert (run / "output" / "out.txt").read_text() == "hi"


def test_the_wrapper_exits_with_the_command_code(in_image):
    assert run_docker.main([str(in_image), "--", sys.executable, "-c", "raise SystemExit(3)"]) == 3


def test_a_missing_program_fails_the_run(in_image):
    with pytest.raises(SystemExit) as exit:
        run_docker.main([str(in_image), "--", "no-such-program-pdt"])
    assert exit.value.code == 127


def test_state_is_kept_only_after_a_run_that_succeeds(in_image):
    program = ("import os, pathlib, sys; p = pathlib.Path(os.environ['PDT_STATE_DIR'], 'n'); "
               "p.write_text(str(int(p.read_text()) + 1) if p.exists() else '1'); "
               "sys.exit(int(sys.argv[1]))")
    state = in_image.parent / ".pdt" / "storage" / "hello" / "state" / "n"
    assert run_docker.main([str(in_image), "--state", "--", sys.executable, "-c", program, "0"]) == 0
    assert run_docker.main([str(in_image), "--state", "--", sys.executable, "-c", program, "0"]) == 0
    assert state.read_text() == "2"
    assert run_docker.main([str(in_image), "--state", "--", sys.executable, "-c", program, "1"]) == 1
    assert state.read_text() == "2"


def test_pdt_run_passes_secrets_by_name_and_mounts_the_folders(in_image, monkeypatch, tmp_path):
    (in_image / "config.yml").write_text("schedule: daily\nenv:\n  required: [API_TOKEN]\n"
                                         "  optional: [CERT_PATH]\n")
    (tmp_path / "cert.pem").write_text("cert")
    monkeypatch.setenv("API_TOKEN", "secret-value")
    monkeypatch.setenv("CERT_PATH", str(tmp_path / "cert.pem"))
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs["env"]))
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(run_docker.subprocess, "run", run)
    assert run_docker.main([str(in_image), "--image", "pdt-hello:local", "--",
                            "node", "hello.js"]) == 0
    (docker, env), = calls
    assert docker[:3] == ["docker", "run", "--rm"]
    assert docker[-4:] == ["--entrypoint", "node", "pdt-hello:local", "hello.js"]
    assert "secret-value" not in " ".join(docker)
    passed = [docker[i + 1] for i, arg in enumerate(docker) if arg == "-e"]
    assert passed == ["API_TOKEN", "CERT_B64", "PDT_CONFIG_JSON", "PDT_RUN_ID", "PDT_OUTPUT_DIR",
                      "PDT_STATE_DIR"]
    mounts = [docker[i + 1].rsplit(":", 1)[1] for i, arg in enumerate(docker) if arg == "-v"]
    assert mounts == ["/pdt/output", "/pdt/state"]
    assert env["API_TOKEN"] == "secret-value" and env["CERT_B64"] == "Y2VydA=="
    assert env["PDT_OUTPUT_DIR"] == "/pdt/output"


def test_new_app_from_the_node_example_lists_its_dockerfile(project, capsys):
    assert scaffold.new_app("greeter", "node-hello-world") == 0
    assert find_apps() == ["greeter"]
    assert config.docker_app(project / "greeter")
    assert "greeter/Dockerfile" in capsys.readouterr().out
    assert validate_app("greeter") == []
