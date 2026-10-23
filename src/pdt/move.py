"""Move apps to another provider, region, or account (`pdt move`, and `pdt init` in a project)."""

from __future__ import annotations

from pathlib import Path

from pdt import config, console, deploy
from pdt.config import APP_FILE, PROJECT_FILE, ConfigError
from pdt.scaffold import (NAMES, PLATFORM_TEMPLATE, PROVIDER_CHOICES, PROVIDER_QUESTIONS,
                          TIMEZONE_QUESTION, _ask, _ask_choice, ask_settings)


def platform_lines(platform: dict) -> list[str]:
    return ["platform:", *(f"  {key}: {config.yaml_quoted(value)}" for key, value in platform.items()
                           if value is not None)]


def replace_platform(text: str, platform: dict, shown: str = PROJECT_FILE) -> str:
    """`text` with its platform: block set to `platform`, or removed when `platform` is empty."""
    import yaml
    end_of_line = "\r" if "\r\n" in text else ""
    lines = text.split("\n")
    new = [line + end_of_line for line in platform_lines(platform)] if platform else []
    root = yaml.compose(text)
    events = list(yaml.parse(text))
    if isinstance(root, yaml.MappingNode) and root.flow_style:
        raise ConfigError(f"the top level of {shown} is in braces, so pdt cannot change "
                          f"it safely. Change platform: in {shown} by hand.")
    if any(isinstance(event, yaml.DocumentEndEvent) and event.explicit for event in events):
        raise ConfigError(f"{shown} has a ... document end line, so pdt cannot change it "
                          f"safely. Change platform: in {shown} by hand.")
    keys = [key for key, _value in (root.value if root is not None else [])]
    found = [(key, value) for key, value in (root.value if root is not None else [])
             if key.value == "platform"]
    if found:
        key, value = found[0]
        following = [other.start_mark.line for other in keys if other.start_mark.line > key.start_mark.line]
        bound = min(following, default=len(lines))
        if isinstance(value, yaml.MappingNode) and not value.flow_style and value.value:
            last = value.value[-1][1].end_mark
        else:
            last = value.end_mark
        start, stop = key.start_mark.line, last.line + (last.column > 0)
        if any(getattr(event, "anchor", None) and start <= event.start_mark.line < bound
               for event in events):
            raise ConfigError(f"platform: in {shown} holds a YAML anchor (&) or alias (*), "
                              f"so pdt cannot change it safely. Change platform: in {shown} "
                              "by hand.")
        lines[start:stop] = new
    elif not new:
        return text
    elif PLATFORM_TEMPLATE + end_of_line in lines:
        start = lines.index(PLATFORM_TEMPLATE + end_of_line)
        stop = start + 1
        while stop < len(lines) and lines[stop].startswith("#  "):
            stop += 1
        lines[start:stop] = new
    elif text == "":
        lines = [*new, ""]
    else:
        if lines[-1] != "":
            lines[-1] += end_of_line
            lines.append("")
        lines[-1:] = [end_of_line, *new, ""]
    return "\n".join(lines)


def read_text(path: Path) -> str:
    return path.read_bytes().decode("utf-8") if path.is_file() else ""


def platform_change(path: Path, text: str, platform: dict) -> tuple[Path, str, str]:
    """The file, its `text`, and that text with the platform: block set to `platform`."""
    bom = "﻿" if text.startswith("﻿") else ""
    shown = path.relative_to(config.find_project()).as_posix()
    return path, text, bom + replace_platform(text.removeprefix(bom), platform, shown)


def own_change(root_cfg: dict, root_platform: dict, name: str, platform: dict) -> tuple[Path, str, str]:
    """The change to `name`/config.yml that deploys the app to `platform` when pdt.yml sets
    `root_platform`: no platform: block when the app gets that place from pdt.yml, else a
    full block."""
    inherited = deploy.resolved({**root_platform,
                                 **(config.root_app_entry(root_cfg, name).get("platform") or {})})
    if not deploy.moved(inherited, platform):
        platform = {key: value for key, value in platform.items()
                    if str(inherited.get(key) or "") != str(value)}
    path = config.find_project() / name / APP_FILE
    return platform_change(path, read_text(path), platform)


def merged_platform(root_cfg: dict, root_platform: dict, app_dir: Path) -> dict:
    return {**root_platform,
            **(config.root_app_entry(root_cfg, app_dir.name).get("platform") or {}),
            **(config.load_yaml(app_dir / APP_FILE).get("platform") or {})}


def pin_problem(path: Path, shown: str) -> str:
    import yaml
    text = path.read_text(encoding="utf-8-sig") if path.is_file() else ""
    root = yaml.compose(text)
    if isinstance(root, yaml.MappingNode) and root.flow_style:
        return (f"the top level of {shown} is in braces, so pdt cannot add platform: to it. "
                f"Write each key of {shown} on its own line, or delete the {{}} when {shown} holds "
                "nothing else.")
    if (isinstance(root, yaml.MappingNode) and any(key.value == "platform" for key, _value in root.value)
            and not any(line.strip() == "platform:" for line in text.splitlines())):
        return (f"platform: in {shown} is not on a line of its own, so pdt cannot change it. "
                f"Write platform: on its own line in {shown}, and each key under it on its own "
                "indented line.")
    return ""


def current(name: str) -> dict:
    """Where the app is deployed, or where its config deploys it when it is not deployed."""
    return config.deployed_platform(name) or deploy.resolved(config.merged_app(name)["platform"])


def same(one: dict, other: dict) -> bool:
    one, other = deploy.resolved(one), deploy.resolved(other)
    return not deploy.moved(one, other) or not deploy.moved(other, one)


def described(platform: dict) -> str:
    place = config.place(platform)
    return deploy.describe_place(place) if place else "no provider"


def pins_for(names: list[str], root_platform: dict) -> dict[str, dict]:
    """The platform: keys each app needs in its config.yml to stay where it is when pdt.yml
    sets `root_platform`."""
    target = config.find_project()
    root_cfg = config.load_yaml(target / PROJECT_FILE)
    root_provider = (root_cfg.get("platform") or {}).get("provider")
    pins = {}
    for name in names:
        recorded = current(name)
        before = deploy.resolved(config.merged_app(name)["platform"])
        after = deploy.resolved(merged_platform(root_cfg, root_platform, target / name))
        profile = recorded.get("profile") or before.get("profile")
        extra = {"profile": profile if recorded.get("provider") == root_provider else None,
                 "timezone": before.get("timezone")}
        settings = {**config.place(recorded), **{key: str(value) for key, value in extra.items() if value}}
        pin = {key: value for key, value in settings.items() if str(after.get(key) or "") != value}
        if not pin.keys() - {"timezone"}:
            continue
        problem = pin_problem(target / name / APP_FILE, f"{name}/{APP_FILE}")
        if problem != "":
            raise ConfigError(problem)
        pins[name] = pin
    return pins


def ask_apps(numbered: list[str]) -> list[str]:
    while True:
        answer = _ask("Type the numbers of the apps to move, separated by spaces "
                      "(Enter cancels)")
        numbers = answer.replace(",", " ").split()
        if all(number.isdigit() and 1 <= int(number) <= len(numbered) for number in numbers):
            return [name for index, name in enumerate(numbered, start=1)
                    if str(index) in numbers]
        console.warn(f"Please type numbers from 1 to {console.value(len(numbered))}.")


def kept_settings(current: dict, provider: str) -> dict:
    if current.get("provider") == provider:
        return dict(current)
    timezone = current.get("timezone")
    if timezone not in (None, "local") and TIMEZONE_QUESTION in PROVIDER_QUESTIONS[provider]:
        return {"timezone": timezone}
    return {}


def ask_place(current: dict) -> dict | None:
    """The provider and its settings, starting from `current`; None when the user cancels."""
    keys = [key for key, _label in PROVIDER_CHOICES[:-1]]
    labels = [label for _key, label in PROVIDER_CHOICES[:-1]] + ["Cancel"]
    provider = str(current.get("provider") or "")
    choice = _ask_choice("Where should your jobs run?", labels,
                         keys.index(provider) + 1 if provider in keys else 1)
    if choice == len(labels):
        return None
    return ask_settings(keys[choice - 1], kept_settings(current, keys[choice - 1]))


def apply(command: str, there: str, moving: list[str], changes: list[tuple[Path, str, str]],
          moved: dict[str, tuple[Path, str, str]], pins: dict[str, dict]) -> int:
    """Show the plan, then destroy each deployed app in `moving`, save `pins` for each app that
    did not move, write `changes` and the `moved` config.yml of each app that moved, and deploy
    each app that was deployed to `there`. 1 when a destroy or a deploy failed."""
    target = config.find_project()
    where = {name: described(current(name)) for name in [*moving, *pins]}
    deployed = [name for name in moving if config.is_deployed(name)]
    files = [change for change in [*changes, *moved.values()] if change[1] != change[2]]
    if deployed:
        console.say()
        deploy.show_plan(
            [console.Markup(f"destroy {console.value(name)} on {console.value(where[name])}")
             for name in deployed]
            + [console.Markup(f"update {console.value(path.relative_to(target).as_posix())}")
               for path, _text, _changed in files]
            + [console.Markup(f"update {console.value(f'{name}/{APP_FILE}')}: {console.value(name)} "
                              f"stays on {console.value(where[name])}")
               for name in pins if name not in moving]
            + [console.Markup(f"deploy {console.value(name)} to {console.value(there)}")
               for name in deployed])
        console.say("Stored data will not be deleted.")
        try:
            answer = console.confirm("Continue?")
        except EOFError:
            answer = False
        if not answer:
            console.say("Cancelled. Nothing changed.")
            return 0
    removed, pinned = [], []
    for name in moving:
        if name in deployed:
            console.say()
            if deploy.destroy(name, assume_yes=True) != 0:
                console.error(f"{console.value(name)} was not removed. "
                              f"It is still deployed to {console.value(where[name])}.")
                break
        removed.append(name)
    console.say()
    for name, pin in pins.items():
        if name in removed:
            continue
        app = config.merged_app(name)
        for key, value in pin.items():
            config.save_platform_key(app, key, value, path=target / name / APP_FILE)
        pinned.append(name)
        console.say(f"{console.value(name)} stays on "
                    f"{console.value(where[name])}. "
                    f"Its {console.value('platform:')} settings are in "
                    f"{console.value(f'{name}/{APP_FILE}')}.")
    for path, text, changed in [*changes, *(moved[name] for name in removed if name in moved)]:
        if changed == text:
            continue
        shown = path.relative_to(target).as_posix()
        with config.locked(path):
            if read_text(path) != text:
                raise ConfigError(f"{shown} changed while pdt {command} was asking; "
                                  f"run pdt {command} again")
            config.write_text_atomically(path, changed, newline="")
        console.done(f"Updated the {console.value('platform:')} settings in {console.value(shown)}.")
    failed = []
    for name in removed:
        if name not in deployed:
            continue
        console.say()
        if deploy.deploy(name, assume_yes=True, run=False) != 0:
            failed.append(name)
    if failed:
        console.say()
        console.next_steps([(f"pdt deploy {name}", f"deploy it to {console.escape(there)}")
                            for name in failed])
    elif not moving and not pinned:
        console.say()
        console.next_steps([("pdt validate", "check every app on the new settings"),
                            ("pdt deploy --all", "deploy every app to "
                                                 f"{console.escape(there)}")])
    return 0 if removed == moving and not failed else 1


def move_project(target: Path) -> int:
    marker = target / PROJECT_FILE
    text = read_text(marker)
    root_cfg = config.load_yaml(marker)
    current_platform = root_cfg.get("platform") or {}
    old = str(current_platform.get("provider") or "")
    console.say(f"{console.value(target)} is already a pdt project.")
    if old != "":
        console.field("Runs on", NAMES.get(old, old))
    deployed = [name for name in config.read_state().get("deployed") or []
                if name in config.app_folders()]
    apps = {name: config.merged_app(name) for name in deployed}
    numbered = [name for name in deployed
                if "provider" not in merged_platform(root_cfg, {}, target / name)]
    recorded = {name: current(name) for name in deployed}
    where = {name: described(recorded[name]) for name in deployed}
    if deployed:
        console.say()
        console.table(["", "app", "where"],
                      [[str(numbered.index(name) + 1), name, where[name]] if name in numbered
                       else ["", name, f"{where[name]}; set in {apps[name]['platform_from']}"]
                       for name in deployed])
    if len(numbered) < len(deployed):
        console.next_steps([(f"pdt move {name}", "") for name in deployed if name not in numbered],
                           "To move an app that sets its own place, run:")
    platform = ask_place(current_platform)
    if platform is None:
        console.say("Cancelled. Nothing changed.")
        return 0
    there = described(deploy.resolved(platform))
    after = {name: merged_platform(root_cfg, platform, target / name) for name in deployed}
    pins = pins_for(deployed, platform)
    movable = [name for name in numbered if deploy.moved(recorded[name], after[name])]
    moving = []
    if movable:
        here = described(deploy.resolved(current_platform))
        choice = _ask_choice(f"What should happen to the apps currently deployed to {console.escape(here)}?",
                             ["Move all", "Move some",
                              f"Keep them on {here} (only new apps go to {there})", "Cancel"], 4)
        if choice == 1:
            moving = movable
        elif choice == 2:
            moving = [name for name in ask_apps(numbered) if name in movable]
        if choice == 4 or choice == 2 and not moving:
            console.say("Cancelled. Nothing changed.")
            return 0
    moved = {name: own_change(root_cfg, platform, name, platform) for name in moving}
    return apply("init", there, moving, [platform_change(marker, text, platform)], moved, pins)


def move_app(name: str) -> int:
    if not deploy.can_prompt(None):
        raise ConfigError("there is no one to answer the questions. Run pdt move in a terminal.")
    marker = config.find_project() / PROJECT_FILE
    text = read_text(marker)
    root_cfg = config.load_yaml(marker)
    default = root_cfg.get("platform") or {}
    app = config.merged_app(name)
    names = config.app_folders()
    now = {other: current(other) for other in names}
    here = described(now[name])
    if config.is_deployed(name):
        console.say(f"{console.value(name)} is deployed to {console.value(here)}.")
    elif "provider" in now[name]:
        console.say(f"{console.value(name)} is not deployed. {console.value(app['platform_from'])} "
                    f"deploys it to {console.value(here)}.")
    else:
        console.say(f"{console.value(name)} has no provider.")
    group = [other for other in names if "provider" in now[name] and same(now[name], now[other])] or [name]
    moving = [name]
    if len(group) > 1:
        console.say()
        console.table(["", "app", "deployed", "settings in"],
                      [[str(number), other, "yes" if config.is_deployed(other) else "no",
                        config.merged_app(other)["platform_from"]]
                       for number, other in enumerate(group, start=1)])
        choice = _ask_choice(f"Which apps should move from {console.escape(here)}?",
                             [f"Only {name}", "Some", f"All {len(group)} apps on {here}", "Cancel"])
        if choice == 2:
            moving = ask_apps(group)
        elif choice == 3:
            moving = group
        if choice == 4 or not moving:
            console.say("Cancelled. Nothing changed.")
            return 0
    platform = ask_place({**config.place(app["platform"]),
                          **{key: app["platform"][key] for key in ("profile", "timezone")
                             if app["platform"].get(key)}})
    if platform is None:
        console.say("Cancelled. Nothing changed.")
        return 0
    there = described(deploy.resolved(platform))
    if "provider" in now[name] and same(now[name], platform):
        console.say(f"{console.value(here)} is the current place. Nothing changed.")
        return 0
    root_platform, pins, changes = default, {}, []
    if not same(deploy.resolved(default), platform):
        pins = pins_for(names, platform)
        console.say()
        if "provider" in default:
            console.say(f"The default platform in {console.value(PROJECT_FILE)} is "
                        f"{console.value(described(deploy.resolved(default)))}.")
        else:
            console.say(f"{console.value(PROJECT_FILE)} sets no default platform.")
        try:
            change = console.confirm(f"Change the default platform to {there}?",
                                     default=not pins.keys() - set(moving))
        except EOFError:
            console.say("Cancelled. Nothing changed.")
            return 0
        if change:
            root_platform, changes = platform, [platform_change(marker, text, platform)]
        else:
            pins = {}
    moved = {other: own_change(root_cfg, root_platform, other, platform) for other in moving}
    return apply("move", there, moving, changes, moved, pins)
