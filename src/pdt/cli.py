"""pdt — set up, run, and deploy scheduled jobs.

Leave <app> off any command that takes one, or mistype it, and pdt lists the apps it found.

Every command except init, examples, completion, aws, az, and gcloud needs a project.
pdt finds it by walking up from the working directory to the nearest pdt.yml.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

import rich_argparse

from pdt import __version__, completion, config, console, deploy, scaffold
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


def say_no_apps() -> None:
    console.say("This project has no apps yet.")
    console.say("Start from an example:")
    console.command("pdt examples")
    console.command("pdt new my-report --from <example>")


def choose_app(name: str | None, command: str) -> str | None:
    """Return the app `pdt <command>` should act on, or None after guiding the user.

    Every command that takes an app name goes through here, so leaving the name
    off or mistyping it gives the same answer everywhere: the apps this project
    has, and the exact command to run next.
    """
    apps = config.find_apps()
    if not apps:
        say_no_apps()
        return None
    if name in apps:
        return name
    if name is None:
        console.heading(f"Which app do you want to {command}? This project has:")
    else:
        console.error(f"no app named {name!r}. This project has:")
    shown = apps[:5]
    for app in shown:
        console.name(app)
    if len(apps) > len(shown):
        console.bullet(f"... and {len(apps) - len(shown)} more")
    console.command("pdt list", "see every app")
    console.command(f"pdt {command} <app>")
    return None


def cmd_list(_args) -> int:
    apps = config.find_apps()
    if not apps:
        say_no_apps()
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
    name = choose_app(args.app, "run")
    if name is None:
        return 1
    app = config.merged_app(name)
    if config.uses_email(app):
        config.load_env(app["dir"])
        problems = email_problems(app["config"], check_oauth=False)
        if problems:
            for problem in problems:
                console.error(f"{name}: {problem}")
            return 1
        prepare_email_auth(auth_env_file(app["dir"]))
    proc = subprocess.run(["uv", "run", "--script", "run.py"], cwd=app["dir"])
    if (proc.returncode == 0 and name == scaffold.STARTER
            and not config.is_deployed(name)):
        console.say()
        console.say("Try deploying this job:")
        console.command(f"pdt deploy {scaffold.STARTER}")
    return proc.returncode


def cmd_deploy(args) -> int:
    name = choose_app(args.app, "deploy")
    if name is None:
        return 1
    return deploy.deploy(name, assume_yes=args.yes, profile=args.profile)


def cmd_login(args) -> int:
    name = choose_app(args.app, "login")
    if name is None:
        return 1
    return deploy.login(name, profile=args.profile)


def cmd_destroy(args) -> int:
    name = choose_app(args.app, "destroy")
    if name is None:
        return 1
    return deploy.destroy(name, assume_yes=args.yes, profile=args.profile)


def cmd_completion(args) -> int:
    return completion.install(args.shell, print_only=args.script)


def build_parser() -> argparse.ArgumentParser:
    summary, _, note = __doc__.strip().partition("\n\n")
    parser = argparse.ArgumentParser(
        prog="pdt", description=summary, epilog=note,
        formatter_class=rich_argparse.RawDescriptionRichHelpFormatter)
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", title="commands", metavar="<command>")

    def add_parser(name: str, **kwargs):
        return sub.add_parser(
            name, formatter_class=rich_argparse.RawDescriptionRichHelpFormatter, **kwargs)

    p = add_parser("init", help="create a project here, or in DIR")
    directory = p.add_argument("directory", nargs="?",
                               help="where to create the project (default: here)")
    directory.completer = completion.directories
    p.add_argument("--yes", action="store_true", help="accept the defaults, ask nothing")
    p.set_defaults(func=cmd_init)
    add_parser("examples", help="list the bundled example apps").set_defaults(
        func=cmd_examples)
    p = add_parser("new", help="add an app to the project")
    p.add_argument("app", help="the app's folder name")
    source = p.add_argument("--from", dest="source",
                            help="which example to copy; run `pdt examples` to see them")
    source.completer = completion.examples
    p.set_defaults(func=cmd_new)
    add_parser("list", help="show every app").set_defaults(func=cmd_list)
    add_parser("validate", help="check config and env").set_defaults(func=cmd_validate)
    p = add_parser("run", help="run an app locally")
    app = p.add_argument("app", nargs="?",
                         help="the app's folder name; omit to see the choices")
    app.completer = completion.apps
    p.set_defaults(func=cmd_run)
    p = add_parser("deploy", help="deploy an app")
    app = p.add_argument("app", nargs="?",
                         help="the app's folder name; omit to see the choices")
    app.completer = completion.apps
    p.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    profile = p.add_argument("--profile", help="AWS profile name (AWS only)")
    profile.completer = completion.profiles
    p.set_defaults(func=cmd_deploy)
    p = add_parser("login", help="sign in again to an app's cloud provider")
    app = p.add_argument("app", nargs="?",
                         help="the app's folder name; omit to see the choices")
    app.completer = completion.apps
    profile = p.add_argument("--profile", help="AWS profile name (AWS only)")
    profile.completer = completion.profiles
    p.set_defaults(func=cmd_login)
    p = add_parser("destroy", help="tear down an app's deployed resources")
    app = p.add_argument("app", nargs="?",
                         help="the app's folder name; omit to see the choices")
    app.completer = completion.apps
    p.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    profile = p.add_argument("--profile", help="AWS profile name (AWS only)")
    profile.completer = completion.profiles
    p.set_defaults(func=cmd_destroy)
    p = add_parser("completion", help="turn on tab completion in your shell")
    p.add_argument("shell", nargs="?", choices=completion.SHELLS,
                   help="which shell; pdt works it out when left off")
    p.add_argument("--script", action="store_true",
                   help="print the completion script instead of installing it")
    p.set_defaults(func=cmd_completion)
    for name, label in (("aws", "AWS"), ("az", "Azure"), ("gcloud", "Google Cloud")):
        p = add_parser(name, help=f"run the {label} CLI that pdt installs")
        p.add_argument("args", nargs=argparse.REMAINDER)
    return parser


def main() -> int:
    parser = build_parser()
    completion.configure(parser)
    if len(sys.argv) > 1 and sys.argv[1] in CLOUD_CLIS:
        # Before argparse, so the cloud CLI parses its own flags.
        script = Path(__file__).with_name(CLOUD_CLIS[sys.argv[1]])
        return subprocess.run(
            ["uv", "run", "--script", str(script), *sys.argv[1:]]).returncode
    args = parser.parse_args()
    if args.command is None:
        parser.print_help()
        return 0
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
