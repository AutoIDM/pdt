"""pdt — set up, run, and deploy scheduled jobs.

Every command except init, examples, aws, az, and gcloud needs a project.
pdt finds it by walking up from the working directory to the nearest pdt.yml.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

import rich_argparse

from pdt import __version__, config, console, deploy, scaffold
from pdt.config import ConfigError
from pdt.utils.send_email import auth_env_file, email_problems, prepare_email_auth

CLOUD_CLIS = {
    "aws": "deploy_aws.py",
    "az": "deploy_azure.py",
    "gcloud": "deploy_google_cloud.py",
}


def cmd_init(args) -> int:
    return scaffold.init(args.directory, args.yes)


def cmd_examples(_args) -> int:
    return scaffold.list_examples()


def cmd_new(args) -> int:
    return scaffold.new_app(args.app, args.source)


def cmd_list(_args) -> int:
    apps = config.find_apps()
    if not apps:
        console.say("This project has no apps yet.")
        console.say("Start from an example:")
        console.command("pdt examples")
        console.command("pdt new my-report --from <example>")
        return 0
    rows = []
    for name in apps:
        try:
            app = config.merged_app(name)
            rows.append([name, app["schedule"] or "-",
                         app["platform"].get("provider", "-")])
        except ConfigError as e:
            rows.append([name, "-", f"config error: {e}"])
    console.table(["App", "Schedule", "Provider"], rows, ["bold cyan"])
    return 0


def cmd_validate(_args) -> int:
    problems = config.validate()
    original_env = os.environ.copy()
    try:
        for name in config.find_apps():
            os.environ.clear()
            os.environ.update(original_env)
            try:
                app = config.merged_app(name)
            except ConfigError:
                continue
            config.load_env(app["dir"])
            for problem in config.check_env(app["env"]):
                problems.append(f"{name}: {problem}")
            if config.uses_email(app):
                for problem in email_problems(app["config"]):
                    problems.append(f"{name}: {problem}")
    finally:
        os.environ.clear()
        os.environ.update(original_env)
    if problems:
        for problem in problems:
            console.error(problem)
        console.failed(f"{len(problems)} problem(s) found.")
        return 1
    console.done("Configuration is valid.")
    return 0


def cmd_run(args) -> int:
    try:
        app = config.merged_app(args.app)
    except ConfigError as e:
        console.error(str(e))
        return 1
    if config.uses_email(app):
        config.load_env(app["dir"])
        problems = email_problems(app["config"], check_oauth=False)
        if problems:
            for problem in problems:
                console.error(f"{args.app}: {problem}")
            return 1
        prepare_email_auth(auth_env_file(app["dir"]))
    proc = subprocess.run(["uv", "run", "--script", "run.py"], cwd=app["dir"])
    if (proc.returncode == 0 and args.app == scaffold.STARTER
            and not config.is_deployed(args.app)):
        console.say()
        console.say("Try deploying this job:")
        console.command(f"pdt deploy {scaffold.STARTER}")
    return proc.returncode


def ask_which_app(command: str) -> int:
    apps = config.find_apps()
    if not apps:
        console.say("This project has no apps yet.")
        console.say("Start from an example:")
        console.command("pdt examples")
        console.command("pdt new my-report --from <example>")
        return 1
    shown = apps[:5]
    console.heading(f"Which app do you want to {command}? This project has:")
    for name in shown:
        console.name(name)
    if len(apps) > len(shown):
        console.bullet(f"... and {len(apps) - len(shown)} more")
    console.command("pdt list", "see every app")
    console.command(f"pdt {command} <app>")
    return 1


def cmd_deploy(args) -> int:
    if args.app is None:
        return ask_which_app("deploy")
    return deploy.deploy(args.app, assume_yes=args.yes, profile=args.profile)


def cmd_login(args) -> int:
    return deploy.login(args.app, profile=args.profile)


def cmd_destroy(args) -> int:
    return deploy.destroy(args.app, assume_yes=args.yes, profile=args.profile)


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] in CLOUD_CLIS:
        # Before argparse, so the cloud CLI parses its own flags.
        script = Path(__file__).with_name(CLOUD_CLIS[sys.argv[1]])
        return subprocess.run(
            ["uv", "run", "--script", str(script), *sys.argv[1:]]).returncode
    summary, _, note = __doc__.strip().partition("\n\n")
    parser = argparse.ArgumentParser(
        prog="pdt", description=summary, epilog=note,
        formatter_class=rich_argparse.RawDescriptionRichHelpFormatter)
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", required=True, title="commands",
                                metavar="<command>")

    def add_parser(name: str, **kwargs):
        return sub.add_parser(
            name, formatter_class=rich_argparse.RawDescriptionRichHelpFormatter, **kwargs)

    p = add_parser("init", help="create a project here, or in DIR")
    p.add_argument("directory", nargs="?", help="where to create the project (default: here)")
    p.add_argument("--yes", action="store_true", help="accept the defaults, ask nothing")
    p.set_defaults(func=cmd_init)
    add_parser("examples", help="list the bundled example apps").set_defaults(
        func=cmd_examples)
    p = add_parser("new", help="add an app to the project")
    p.add_argument("app", help="the app's folder name")
    p.add_argument("--from", dest="source",
                   help="which example to copy; run `pdt examples` to see them")
    p.set_defaults(func=cmd_new)
    add_parser("list", help="show every app").set_defaults(func=cmd_list)
    add_parser("validate", help="check config and env").set_defaults(func=cmd_validate)
    p = add_parser("run", help="run an app locally")
    p.add_argument("app", help="the app's folder name")
    p.set_defaults(func=cmd_run)
    p = add_parser("deploy", help="deploy an app")
    p.add_argument("app", nargs="?", help="the app's folder name; omit to see the choices")
    p.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    p.add_argument("--profile", help="AWS profile name (AWS only)")
    p.set_defaults(func=cmd_deploy)
    p = add_parser("login", help="sign in again to an app's cloud provider")
    p.add_argument("app", help="the app's folder name")
    p.add_argument("--profile", help="AWS profile name (AWS only)")
    p.set_defaults(func=cmd_login)
    p = add_parser("destroy", help="tear down an app's deployed resources")
    p.add_argument("app", help="the app's folder name")
    p.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    p.add_argument("--profile", help="AWS profile name (AWS only)")
    p.set_defaults(func=cmd_destroy)
    for name, label in (("aws", "AWS"), ("az", "Azure"), ("gcloud", "Google Cloud")):
        add_parser(name, help=f"run the {label} CLI that pdt installs")
    args = parser.parse_args()
    try:
        return args.func(args)
    except ConfigError as e:
        console.error(str(e))
        return 1
    except KeyboardInterrupt:
        console.say()
        return 130


if __name__ == "__main__":
    sys.exit(main())
