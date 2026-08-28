"""pdt — set up, run, and deploy scheduled jobs.

Commands:
  init [DIR]             create a project here, or in DIR
  examples               list the example apps bundled with pdt
  new <app> --from EXAMPLE   add an app to the project
  list                   show every app with its schedule and provider
  validate               check config files and required env vars
  run <app>              run an app locally
  deploy <app> [--yes] [--profile NAME]   deploy an app to its configured platform
  destroy <app> [--yes] [--profile NAME]  tear down everything deploy created for an app
  login <app> [--profile NAME]            sign in again to the app's cloud provider
  az <args...>           run the Azure CLI that pdt installs
  gcloud <args...>       run the Google Cloud CLI that pdt installs

Every command except `init`, `examples`, `az`, and `gcloud` needs a project.
pdt finds it by walking up from the working directory to the nearest pdt.yml.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

from pdt import __version__, config, deploy, scaffold
from pdt.config import ConfigError
from pdt.utils.send_email import email_problems, prepare_email_auth

CLOUD_CLIS = {"az": "deploy_azure.py", "gcloud": "deploy_google_cloud.py"}


def cmd_init(args) -> int:
    return scaffold.init(args.directory, args.yes)


def cmd_examples(_args) -> int:
    return scaffold.list_examples()


def cmd_new(args) -> int:
    return scaffold.new_app(args.app, args.source)


def cmd_list(_args) -> int:
    apps = config.find_apps()
    if not apps:
        print("This project has no apps yet.")
        print("Run `pdt examples` to see what you can start from,")
        print("then `pdt new my-report --from <example>`.")
        return 0
    for name in apps:
        try:
            app = config.merged_app(name)
            schedule = app["schedule"] or "-"
            provider = app["platform"].get("provider", "-")
            print(f"{name}  schedule={schedule}  provider={provider}")
        except ConfigError as e:
            print(f"{name}  config error: {e}")
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
            print(f"error: {problem}")
        print(f"{len(problems)} problem(s) found.")
        return 1
    print("Configuration is valid.")
    return 0


def cmd_run(args) -> int:
    try:
        app = config.merged_app(args.app)
    except ConfigError as e:
        print(f"error: {e}")
        return 1
    if config.uses_email(app):
        env_files = config.load_env(app["dir"])
        problems = email_problems(app["config"], check_oauth=False)
        if problems:
            for problem in problems:
                print(f"error: {args.app}: {problem}")
            return 1
        prepare_email_auth(env_files[0] if env_files else app["dir"] / ".env")
    proc = subprocess.run(["uv", "run", "--script", "run.py"], cwd=app["dir"])
    return proc.returncode


def cmd_deploy(args) -> int:
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
    parser = argparse.ArgumentParser(
        prog="pdt", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("init", help="create a project here, or in DIR")
    p.add_argument("directory", nargs="?")
    p.add_argument("--yes", action="store_true", help="accept the defaults, ask nothing")
    p.set_defaults(func=cmd_init)
    sub.add_parser("examples", help="list the bundled example apps").set_defaults(
        func=cmd_examples)
    p = sub.add_parser("new", help="add an app to the project")
    p.add_argument("app")
    p.add_argument("--from", dest="source",
                   help="which example to copy; run `pdt examples` to see them")
    p.set_defaults(func=cmd_new)
    sub.add_parser("list", help="show every app").set_defaults(func=cmd_list)
    sub.add_parser("validate", help="check config and env").set_defaults(func=cmd_validate)
    p = sub.add_parser("run", help="run an app locally")
    p.add_argument("app")
    p.set_defaults(func=cmd_run)
    p = sub.add_parser("deploy", help="deploy an app")
    p.add_argument("app")
    p.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    p.add_argument("--profile", help="AWS profile name (AWS only)")
    p.set_defaults(func=cmd_deploy)
    p = sub.add_parser("login", help="sign in again to an app's cloud provider")
    p.add_argument("app")
    p.add_argument("--profile", help="AWS profile name (AWS only)")
    p.set_defaults(func=cmd_login)
    p = sub.add_parser("destroy", help="tear down an app's deployed resources")
    p.add_argument("app")
    p.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    p.add_argument("--profile", help="AWS profile name (AWS only)")
    p.set_defaults(func=cmd_destroy)
    args = parser.parse_args()
    try:
        return args.func(args)
    except ConfigError as e:
        print(f"error: {e}")
        return 1
    except KeyboardInterrupt:
        print()
        return 130


if __name__ == "__main__":
    sys.exit(main())
