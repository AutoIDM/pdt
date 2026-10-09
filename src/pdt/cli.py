"""pdt — set up, run, and deploy scheduled jobs.

Inside an app folder, leave <app> off any command that takes one and pdt uses that app.
Anywhere else, leave it off or mistype it and pdt lists the apps it found.

Every command except init, examples, completion, aws, az, and gcloud needs a project.
pdt finds it by walking up from the working directory to the nearest pdt.yml.
"""

from __future__ import annotations

import argparse
import difflib
import os
import re
import subprocess
import sys
from pathlib import Path

import rich_argparse

from pdt import (__version__, completion, config, console, deploy, deploy_common, gui_cli,
                 powershell, pwsh, scaffold, storage_cli)
from pdt.config import ConfigError
from pdt.utils.email_auth import can_prompt
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
    if args.from_scripts:
        return scaffold.from_scripts(args.app)
    return scaffold.new_app(args.app, args.source)


def say_no_apps() -> None:
    console.say("This project has no apps yet.")
    console.next_steps([("pdt examples", "list the examples"),
                        ("pdt new my-report --from <example>", "copy one into this project")],
                       "Start from an example:")


APP_QUESTIONS = {
    "run": "Which app do you want to run?",
    "deploy": "Which app do you want to deploy?",
    "login": "Which app's platform do you want to sign in to?",
    "destroy": "Which app do you want to destroy?",
    "secrets": "Which app's secrets?",
    "storage": "Which app's files do you want to manage?",
    "runs": "Which app's runs do you want to see?",
    "logs": "Which app's log do you want to read?",
    "health": "Which app do you want to check?",
    "pause": "Which app do you want to pause?",
    "unpause": "Which app do you want to unpause?",
}

SINCE_HELP = ("show every run that started at or after this: 12h, 3d, 2w, 2026-09-20, "
              "or 2026-09-20T14:00 (default: the 10 newest runs)")
SPAN_HELP = "with --since, show only the runs that started within this long after it: 12h, 3d, 2w"
COUNT_HELP = "show at most this many runs (default: 10 without --since, else every run)"
APP_HELP = ("the app's folder name; leave it off inside an app folder to use that app, "
            "or anywhere else to see the choices")


def choose_app(name: str | None, command: str, quiet: bool = False) -> str | None:
    """Return the app `pdt <command>` should act on, or None after guiding the user.

    Every command that takes an app name goes through here. Inside an app folder
    a missing name means that app. Elsewhere, leaving the name off or mistyping
    it gives the same answer everywhere: the apps this project has, and the
    exact command to run next. `quiet` keeps --json output free of the line
    that names the app pdt picked.
    """
    apps = config.find_apps()
    if not apps:
        say_no_apps()
        return None
    if name in apps:
        return name
    here = config.current_app() if name is None else None
    if here is not None:
        if not quiet:
            console.status(f"Using app {console.value(here)} (current folder).")
        return here
    if name is None:
        console.heading(f"{APP_QUESTIONS[command]} This project has:")
    else:
        hint = did_you_mean(name, apps, f"pdt {command} {{}}")
        console.error(f"no app named {console.value(repr(name))}.{console.escape(hint)} "
                      "This project has:")
    shown = apps[:5]
    for app in shown:
        console.name(app)
    if len(apps) > len(shown):
        console.bullet(f"... and {len(apps) - len(shown)} more")
    console.command("pdt list", "see every app")
    console.command(f"pdt {command} <app>")
    return None


def cmd_list(args) -> int:
    if args.names:
        for name in config.find_apps():
            console.say(console.value(name))
        return 0
    apps = config.app_folders()
    if not apps:
        say_no_apps()
        return 0
    rows = []
    for name in apps:
        enabled = "true" if config.is_enabled(name) else "false"
        try:
            app = config.merged_app(name)
            rows.append([name, app["schedule"] or "-",
                         app["platform"].get("provider", "-"), enabled,
                         "true" if app["pause"] else "false"])
        except ConfigError as e:
            rows.append([name, "-", f"config error: {e}", enabled, "-"])
    console.table(["name", "schedule", "platform", "enabled", "paused"], rows, ["bold cyan"])
    return 0


def cmd_validate(_args) -> int:
    console.styled(config.project_line())
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
            sources = config.env_file_lines(app["dir"])
            config.load_env(app["dir"])
            if config.powershell_scripts(app["dir"]):
                problems.extend(powershell_problems(name, app))
            else:
                console.name(name)
                console.detail(f"runs {console.value('run.py')}")
            try:
                found = config.found_env_lines(app)
            except ConfigError:
                found = []
            for line in [*found, *sources]:
                console.detail(line)
            missing = config.missing_env(app)
            if missing != "":
                problems.append(f"{name}: {missing}")
            if config.uses_email(app):
                for problem in email_problems(app["config"]):
                    problems.append(f"{name}: {problem}")
    finally:
        os.environ.clear()
        os.environ.update(original_env)
    if problems:
        for problem in problems:
            console.error(console.escape(problem))
        console.failed(f"{len(problems)} problem(s) found.")
        return 1
    console.done("Configuration is valid.")
    return 0


def powershell_problems(name: str, app: dict) -> list[str]:
    """Scan a PowerShell app, print what it runs and needs, and return the certain findings."""
    console.status(f"Scanning the PowerShell scripts in {console.value(name)}...")
    console.name(name)
    try:
        scan = powershell.scan(app, app["platform"].get("provider", ""))
    except (pwsh.PwshError, powershell.PowerShellError) as e:
        return [f"{name}: {e}"]
    for line in powershell.summary_lines(scan):
        console.detail(line)
    problems, warnings = powershell.report(scan)
    for warning in warnings:
        console.warn(f"{console.value(name)}: {console.escape(warning)}")
    return [f"{name}: {problem}" for problem in problems]


def cmd_run(args) -> int:
    name = choose_app(args.app, "run")
    if name is None:
        return 1
    if args.deployed:
        return deploy.start(name)
    app = config.merged_app(name)
    sources = config.env_file_lines(app["dir"])
    config.load_env(app["dir"])
    missing = config.missing_env(app)
    if missing != "":
        console.error(f"{console.value(name)}: {console.escape(missing)}")
        return 1
    if config.uses_email(app):
        problems = email_problems(app["config"], check_oauth=False)
        if problems:
            for problem in problems:
                console.error(f"{console.value(name)}: {console.escape(problem)}")
            return 1
        prepare_email_auth(auth_env_file(app["dir"]))
    console.name(name)
    for line in [*run_lines(app), *sources]:
        console.detail(line)
    if (app["dir"] / "run.py").is_file():
        proc = subprocess.run(["uv", "run", "--script", "run.py"], cwd=app["dir"])
    else:
        wrapper = Path(__file__).with_name("run_powershell.py")
        proc = subprocess.run(
            ["uv", "run", "--project", str(Path.cwd()), "--script", str(wrapper), str(app["dir"])],
            cwd=app["dir"], env=dict(os.environ, PDT_PROJECT=str(config.find_project())))
    if (proc.returncode == 0 and name == scaffold.STARTER
            and not config.is_deployed(name)):
        console.say()
        console.say("Try deploying this job:")
        console.command(f"pdt deploy {scaffold.STARTER}")
    return proc.returncode


def run_lines(app: dict) -> list[str]:
    """What `pdt run` starts: run.py, or the PowerShell entry scripts and why."""
    if (app["dir"] / "run.py").is_file():
        return [f"runs {console.value('run.py')}"]
    files = powershell.extract(app["dir"])["files"]
    entries, helpers = powershell.split_files(files, app["run_scripts"])
    return powershell.script_lines(entries, helpers,
                                   powershell.entry_rule(files, app["run_scripts"]))


def cmd_deploy(args) -> int:
    if args.skip_failures and not args.all:
        console.error("--skip-failures only works with --all")
        return 1
    if args.all:
        if args.app is not None:
            console.error("pick an app or --all, not both")
            return 1
        return deploy_all(args.yes, args.skip_failures)
    name = choose_app(args.app, "deploy")
    if name is None:
        return 1
    return deploy.deploy(name, assume_yes=args.yes)


def deploy_all(assume_yes: bool, skip_failures: bool) -> int:
    names = config.find_apps()
    if not names:
        say_no_apps()
        return 1
    ask = not skip_failures and can_prompt(None)
    failed: list[str] = []
    original_env = os.environ.copy()
    try:
        for index, name in enumerate(names, 1):
            os.environ.clear()
            os.environ.update(original_env)
            console.heading(f"Deploying {console.value(name)} ({index} of {len(names)})")
            code = deploy.deploy(name, assume_yes=assume_yes)
            if code == 0:
                continue
            failed.append(name)
            console.error(f"{console.value(name)} did not deploy.")
            if index < len(names) and not skip_failures and not (
                    ask and console.confirm(
                        f"Skip the failing app {name} and deploy the rest?")):
                console.say("Fix the problem above and run pdt deploy --all again, "
                            "or add --skip-failures to go on past it.")
                return code
            if ask and not assume_yes and console.confirm(
                    f"Disable the failing app {name}?"):
                path = config.set_app_enabled(name, False)
                console.done(f"Disabled {console.value(name)} in "
                             f"{console.value(path.relative_to(config.find_project()))}. "
                             "Set enabled: true there to bring it back.")
    finally:
        os.environ.clear()
        os.environ.update(original_env)
    if failed:
        console.warn(f"Not deployed: {', '.join(console.value(name) for name in failed)}")
        return 1
    return 0


def cmd_login(args) -> int:
    name = choose_app(args.app, "login")
    if name is None:
        return 1
    return deploy.login(name)


def cmd_destroy(args) -> int:
    name = choose_app(args.app, "destroy")
    if name is None:
        return 1
    return deploy.destroy(name, assume_yes=args.yes)


def use_app_folder(args) -> None:
    """Inside an app folder, read `pdt logs 3` as `pdt logs <that app> 3`.

    argparse fills the app slot first, so a value meant for the next slot lands
    in it. Move that value along when it names no app but fits the next slot.
    """
    value = args.app
    if value is None or value in config.find_apps() or config.current_app() is None:
        return
    if args.command == "secrets" and value in deploy_common.SECRET_ACTIONS and args.name is None:
        args.action, args.name = value, args.action
    elif args.command == "logs" and value.isdigit() and args.number is None:
        args.number = int(value)
    elif args.command == "storage" and value in storage_cli.COMMANDS:
        args.rest = [value, *args.rest]
    else:
        return
    args.app = None


def cmd_pause(args) -> int:
    name = choose_app(args.app, "pause")
    if name is None:
        return 1
    return deploy.pause(name, True)


def cmd_unpause(args) -> int:
    name = choose_app(args.app, "unpause")
    if name is None:
        return 1
    return deploy.pause(name, False)


def cmd_secrets(args) -> int:
    if args.app in deploy_common.SECRET_ACTIONS and args.action in config.find_apps():
        args.app, args.action = args.action, args.app
    use_app_folder(args)
    action = args.action or "diff"
    if action not in deploy_common.SECRET_ACTIONS:
        hint = did_you_mean(action, deploy_common.SECRET_ACTIONS,
                            f"pdt secrets {args.app or '<app>'} {{}}")
        choices = ", ".join(console.value(choice) for choice in deploy_common.SECRET_ACTIONS)
        console.error(f"no secrets action named {console.value(repr(action))}; "
                      f"choose {choices}.{console.escape(hint)}")
        return 1
    name = choose_app(args.app, "secrets")
    if name is None:
        return 1
    return deploy.secrets(name, action, assume_yes=args.yes, name=args.name)


def cmd_storage(args) -> int:
    use_app_folder(args)
    name = choose_app(args.app, "storage")
    if name is None:
        return 1
    return deploy.storage(name, args.rest)


def window_options(args) -> list[str]:
    return [part for option, value in (("--since", args.since), ("--span", args.span),
                                       ("--count", args.count)) if value is not None
            for part in (option, str(value))]


def cmd_runs(args) -> int:
    name = choose_app(args.app, "runs", quiet=args.json)
    if name is None:
        return 1
    return deploy.runs(name, [*window_options(args), *(["--json"] if args.json else [])])


def cmd_logs(args) -> int:
    use_app_folder(args)
    name = choose_app(args.app, "logs", quiet=args.json)
    if name is None:
        return 1
    flags = [flag for flag, on in (("--failed", args.failed), ("--errors", args.errors),
                                   ("--head", args.head), ("--full", args.full),
                                   ("--follow", args.follow), ("--json", args.json)) if on]
    lines = [] if args.lines is None else ["--lines", str(args.lines)]
    number = 1 if args.number is None else args.number
    run_id = [part for wanted in args.id or [] for part in ("--id", wanted)]
    return deploy.logs(name, [str(number), *run_id, *window_options(args), *lines, *flags])


def cmd_health(args) -> int:
    if args.all and args.app is not None:
        console.error("pick an app or --all, not both")
        return 1
    if args.app is None and (args.all or config.current_app() is None):
        names = config.find_apps()
        if not names:
            say_no_apps()
            if args.json:
                console.data("[]")
            return 0
    else:
        name = choose_app(args.app, "health", quiet=args.json)
        if name is None:
            return 1
        if args.app is None and not args.json:
            console.command("pdt health --all", "check every app")
        names = [name]
    return deploy.health(names, args.json)


def cmd_gui(args) -> int:
    return gui_cli.main(args.port, args.background, args.no_browser, args.stop)


def cmd_completion(args) -> int:
    return completion.install(args.shell, print_only=args.script)


def did_you_mean(word: str, choices, hint: str) -> str:
    """" Did you mean `<hint>`?" with the choice closest to a mistyped `word` put in
    `hint`'s `{}`, or "" when no choice is close. Every typo hint in pdt comes from here."""
    match = difflib.get_close_matches(word, list(choices), n=1)
    if not match:
        return ""
    return f" Did you mean `{hint.format(match[0])}`?"


class Parser(argparse.ArgumentParser):
    """An ArgumentParser whose errors name the closest command or option:
    `pdt lgos` says "Did you mean `pdt logs`?"."""

    commands: dict[str, argparse.ArgumentParser] = {}

    def parse_args(self, args=None, namespace=None):
        args, extras = self.parse_known_args(args, namespace)
        if extras:
            command = self.commands.get(getattr(args, "command", None), self)
            hint = did_you_mean(extras[0], command._option_string_actions, "{}")
            self.error(f"unrecognized arguments: {' '.join(extras)}.{hint}")
        return args

    def error(self, message):
        typo = re.search(r"invalid choice: '([^']*)'", message)
        if typo is not None:
            choices = [choice for action in self._actions for choice in action.choices or []]
            message += "." + did_you_mean(typo.group(1), choices, f"{self.prog} {{}}")
        super().error(message)


# Capitalize only the first letter of a section title, so "Cloud CLIs" keeps its case.
rich_argparse.RawDescriptionRichHelpFormatter.group_name_formatter = (
    lambda title: title[:1].upper() + title[1:])

COMMAND_GROUPS = {
    "Get started": ["init", "examples", "new", "completion"],
    "Run and validate": ["list", "validate", "run"],
    "Deploy": ["deploy", "destroy", "login", "pause", "unpause"],
    "Check a deployed app": ["health", "runs", "logs", "gui"],
    "Manage app data": ["secrets", "storage"],
    "Cloud CLIs": ["aws", "az", "gcloud"],
}


def build_parser() -> argparse.ArgumentParser:
    summary, _, note = __doc__.strip().partition("\n\n")
    parser = Parser(
        prog="pdt", description=summary, epilog=note, usage="pdt [-h] [--version] <command> ...",
        formatter_class=rich_argparse.RawDescriptionRichHelpFormatter)
    parser.add_argument("--version", action="version", version=__version__)
    # The commands are listed under COMMAND_GROUPS instead of the one argparse section.
    sub = parser.add_subparsers(dest="command", metavar="<command>", help=argparse.SUPPRESS,
                                prog="pdt")

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
    start = p.add_mutually_exclusive_group()
    source = start.add_argument("--from", dest="source",
                                help="which example to copy; run `pdt examples` to see them")
    source.completer = completion.examples
    start.add_argument("--from-scripts", action="store_true",
                       help="write requirements.psd1 and run.py for the .ps1 files already in APP/, "
                            "to edit how they run")
    p.set_defaults(func=cmd_new)
    p = add_parser("list", help="show every app")
    p.add_argument("--names", action="store_true",
                   help="print only the name of each enabled app, one per line")
    p.set_defaults(func=cmd_list)
    add_parser("validate", help="check config and env").set_defaults(func=cmd_validate)
    p = add_parser("run", help="run an app locally")
    app = p.add_argument("app", nargs="?", help=APP_HELP)
    app.completer = completion.apps
    p.add_argument("--deployed", action="store_true",
                   help="start the deployed job now, instead of running the app on this machine")
    p.set_defaults(func=cmd_run)
    p = add_parser("deploy", help="deploy an app")
    app = p.add_argument("app", nargs="?", help=APP_HELP)
    app.completer = completion.apps
    p.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    p.add_argument("--all", action="store_true", help="deploy every enabled app, in order")
    p.add_argument("--skip-failures", action="store_true",
                   help="with --all, go on past an app that fails to deploy instead of asking")
    p.set_defaults(func=cmd_deploy)
    p = add_parser("login", help="sign in again to an app's platform")
    app = p.add_argument("app", nargs="?", help=APP_HELP)
    app.completer = completion.apps
    p.set_defaults(func=cmd_login)
    p = add_parser("destroy", help="delete an app's deployed resources")
    app = p.add_argument("app", nargs="?", help=APP_HELP)
    app.completer = completion.apps
    p.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    p.set_defaults(func=cmd_destroy)
    p = add_parser("pause", help="stop an app's schedule from starting runs")
    app = p.add_argument("app", nargs="?", help=APP_HELP)
    app.completer = completion.apps
    p.set_defaults(func=cmd_pause)
    p = add_parser("unpause", help="let a paused app's schedule start runs again")
    app = p.add_argument("app", nargs="?", help=APP_HELP)
    app.completer = completion.apps
    p.set_defaults(func=cmd_unpause)
    p = add_parser("secrets", help="compare, send, or fetch a deployed app's .env values")
    app = p.add_argument("app", nargs="?", help=APP_HELP)
    app.completer = completion.apps
    action = p.add_argument("action", nargs="?",
                            metavar="{" + ",".join(deploy_common.SECRET_ACTIONS) + "}",
                            help="diff shows what save would change (the default); "
                                 "save sends your .env values to the deployed app; "
                                 "get copies the deployed values into a file; "
                                 "set NAME puts one value, read from stdin, into the deployed app")
    action.completer = completion.secret_actions
    p.add_argument("name", nargs="?", help="the env var that set changes")
    p.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    p.set_defaults(func=cmd_secrets)
    p = add_parser("storage", help="read or manage an app's data store")
    app = p.add_argument("app", nargs="?", help=APP_HELP)
    app.completer = completion.apps
    rest = p.add_argument("rest", nargs=argparse.REMAINDER, help="ls|get|query|unlock|destroy [args...]")
    rest.completer = completion.storage_args
    p.set_defaults(func=cmd_storage)
    p = add_parser("runs", help="list a deployed app's recent runs")
    app = p.add_argument("app", nargs="?", help=APP_HELP)
    app.completer = completion.apps
    p.add_argument("--since", help=SINCE_HELP)
    p.add_argument("--span", help=SPAN_HELP)
    p.add_argument("--count", type=int, help=COUNT_HELP)
    p.add_argument("--json", action="store_true", help="print JSON for a script or an agent")
    p.set_defaults(func=cmd_runs)
    p = add_parser("logs", help="read the log of one of a deployed app's runs")
    app = p.add_argument("app", nargs="?", help=APP_HELP)
    app.completer = completion.apps
    p.add_argument("number", nargs="?", type=int,
                   help="which run, as `pdt runs` numbers them (default: 1, the newest)")
    p.add_argument("--id", action="append",
                   help="which run, by the id `pdt runs` shows, instead of a number; "
                        "repeat it to read several runs at once")
    p.add_argument("--since", help=SINCE_HELP)
    p.add_argument("--span", help=SPAN_HELP)
    p.add_argument("--count", type=int, help=COUNT_HELP)
    p.add_argument("--failed", action="store_true", help="read the newest failed run")
    p.add_argument("--errors", action="store_true",
                   help="leave out DEBUG and INFO lines")
    p.add_argument("--lines", type=int, help="print this many lines (default: 20)")
    p.add_argument("--head", action="store_true",
                   help="print the first lines, not the last")
    p.add_argument("--full", action="store_true",
                   help="print every line, not only the last 20")
    p.add_argument("--follow", action="store_true",
                   help="keep printing new lines until the run ends and its log is complete")
    p.add_argument("--json", action="store_true", help="print JSON for a script or an agent")
    p.set_defaults(func=cmd_logs)
    p = add_parser("health", help="show whether each deployed app's last run succeeded")
    app = p.add_argument("app", nargs="?",
                         help="one app's folder name; leave it off inside an app folder to "
                              "check that app, or anywhere else to check every app")
    app.completer = completion.apps
    p.add_argument("--all", action="store_true",
                   help="check every enabled app, even inside an app folder")
    p.add_argument("--json", action="store_true", help="print JSON for a script or an agent")
    p.set_defaults(func=cmd_health)
    p = add_parser("gui", help="open the project's dashboard in your web browser")
    p.add_argument("--port", type=int, default=gui_cli.DEFAULT_PORT,
                   help=f"the local port to serve on (default: {gui_cli.DEFAULT_PORT})")
    p.add_argument("--background", action="store_true",
                   help="leave the server running after this command returns; "
                        "stop it with pdt gui --stop")
    p.add_argument("--no-browser", action="store_true",
                   help="start the server without opening a browser")
    p.add_argument("--stop", action="store_true",
                   help="stop a server started with --background")
    p.set_defaults(func=cmd_gui)
    p = add_parser("completion", help="turn on tab completion in your shell")
    p.add_argument("shell", nargs="?", choices=completion.SHELLS,
                   help="which shell; pdt works it out when left off")
    p.add_argument("--script", action="store_true",
                   help="print the completion script instead of installing it")
    p.set_defaults(func=cmd_completion)
    for name, label in (("aws", "AWS"), ("az", "Azure"), ("gcloud", "Google Cloud")):
        p = add_parser(name, help=f"run the {label} CLI that pdt installs")
        args = p.add_argument("args", nargs=argparse.REMAINDER)
        args.completer = completion.files
    parser.commands = sub.choices
    entries = {a.dest: a for a in sub._choices_actions}
    for title, names in COMMAND_GROUPS.items():
        group = parser.add_argument_group(title)
        for name in names:
            group._group_actions.append(entries[name])
    return parser


def main() -> int:
    parser = build_parser()
    completion.configure(parser)
    if len(sys.argv) > 1 and sys.argv[1] in CLOUD_CLIS:
        # Before argparse, so the cloud CLI parses its own flags. --project keeps
        # `uvx pdt aws` working; deploy.provider_command explains why.
        script = Path(__file__).with_name(CLOUD_CLIS[sys.argv[1]])
        deploy.announce_install(script)
        return subprocess.run(
            ["uv", "run", "--project", str(Path.cwd()), "--script", str(script),
             *sys.argv[1:]]).returncode
    args = parser.parse_args()
    if args.command is None:
        parser.print_help()
        return 0
    if getattr(args, "json", False):
        console.to_stderr()
    try:
        return args.func(args)
    except ConfigError as e:
        console.error(console.escape(str(e)))
        return 1
    except KeyboardInterrupt:
        console.say()
        return 130


if __name__ == "__main__":
    sys.exit(main())
