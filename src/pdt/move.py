"""Move apps to another provider, region, or account (`pdt move`)."""

from __future__ import annotations

from pathlib import Path

from pdt import config, console, deploy
from pdt.config import APP_FILE, PROJECT_FILE, ConfigError
from pdt.scaffold import (PLATFORM_TEMPLATE, PROVIDER_CHOICES, PROVIDER_QUESTIONS,
                          TIMEZONE_QUESTION, NoAnswer, _ask, _ask_choice, ask_settings)

NO_TERMINAL = "there is no one to answer the questions. Run pdt move in a terminal."


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
    """The change to `name`/config.yml whose platform: block sets `platform` when pdt.yml sets
    `root_platform`: only the keys that differ from pdt.yml when the app gets its place from
    pdt.yml, else every key. No change when the block already sets those keys."""
    from_pdt_yml = {**root_platform, **(config.root_app_entry(root_cfg, name).get("platform") or {})}
    inherited = deploy.resolved(from_pdt_yml)
    if not deploy.moved(inherited, {**from_pdt_yml, **platform}):
        platform = {key: value for key, value in platform.items()
                    if str(inherited.get(key) or "") != str(value)}
    path = config.find_project() / name / APP_FILE
    text = read_text(path)
    own = config.load_yaml(path).get("platform") or {}
    if ({key: str(value) for key, value in platform.items() if value is not None}
            == {key: str(value) for key, value in own.items() if value is not None}):
        return path, text, text
    return platform_change(path, text, platform)


def merged_platform(root_cfg: dict, root_platform: dict, app_dir: Path) -> dict:
    return {**root_platform,
            **(config.root_app_entry(root_cfg, app_dir.name).get("platform") or {}),
            **(config.load_yaml(app_dir / APP_FILE).get("platform") or {})}


def current(name: str) -> dict:
    """Where the app is deployed, or where its config deploys it when it is not deployed."""
    return config.deployed_platform(name) or deploy.resolved(config.merged_app(name)["platform"])


def same(one: dict, other: dict) -> bool:
    one, other = deploy.resolved(one), deploy.resolved(other)
    return (str(one.get("profile") or "") == str(other.get("profile") or "")
            and (not deploy.moved(one, other) or not deploy.moved(other, one)))


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
        if pin:
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


def check_unchanged(path: Path, text: str) -> None:
    if read_text(path) != text:
        raise ConfigError(f"{path.relative_to(config.find_project()).as_posix()} changed while "
                          "pdt move was asking; run pdt move again")


def apply(root_cfg: dict, root_platform: dict, platform: dict, moving: list[str],
          changes: list[tuple[Path, str, str]], pins: dict[str, dict]) -> int:
    """Check each app in `moving` against `platform` and show the plan. Then destroy each app in
    `moving` deployed to another place, write `changes`, give each app the shortest platform:
    block that keeps it on `platform` (an app in `moving`) or where `pins` keeps it (any other
    app) when pdt.yml sets `root_platform`, and deploy each app that was destroyed.
    1 when a check, a destroy, or a deploy failed."""
    target = config.find_project()
    there = described(deploy.resolved(platform))
    names = config.app_folders()
    where = {name: described(current(name)) for name in names}
    final = {name: {**root_platform, **(config.root_app_entry(root_cfg, name).get("platform") or {}),
                    **platform} for name in moving}
    problems = [problem for name in moving for problem in config.validate_app(name, final[name])]
    if problems:
        for problem in problems:
            console.error(console.escape(problem))
        console.say("Nothing changed.")
        return 1
    moves = {name: own_change(root_cfg, root_platform, name, platform) for name in moving}
    stays = {name: own_change(root_cfg, root_platform, name,
                              {**(config.load_yaml(target / name / APP_FILE).get("platform") or {}),
                               **pins.get(name, {})})
             for name in names}
    deployed = [name for name in moving
                if config.is_deployed(name) and deploy.moved(current(name), final[name])]
    files = [change for change in [*changes, *moves.values()] if change[1] != change[2]]
    others = [name for name in names if name not in moving and stays[name][1] != stays[name][2]]
    if not deployed and not files and not others:
        console.say(f"{console.value(there)} is the current place. Nothing changed.")
        return 0
    console.say()
    deploy.show_plan(
        [console.Markup(f"destroy {console.value(name)} on {console.value(where[name])}")
         for name in deployed]
        + [console.Markup(f"update {console.value(path.relative_to(target).as_posix())}")
           for path, _text, _changed in files]
        + [console.Markup(f"update {console.value(f'{name}/{APP_FILE}')}"
                          + (f": {console.value(name)} stays on {console.value(where[name])}"
                             if name in pins else ""))
           for name in others]
        + [console.Markup(f"deploy {console.value(name)} to {console.value(there)}")
           for name in deployed])
    if deployed:
        console.say("Stored data will not be deleted.")
    try:
        answer = console.confirm("Continue?")
    except EOFError:
        answer = False
    if not answer:
        console.say("Cancelled. Nothing changed.")
        return 0
    for path, text, changed in [*changes, *moves.values(), *stays.values()]:
        if changed != text:
            check_unchanged(path, text)
    removed = []
    for name in moving:
        if name in deployed:
            console.say()
            if deploy.destroy(name, assume_yes=True) != 0:
                console.error(f"{console.value(name)} was not removed. "
                              f"It is still deployed to {console.value(where[name])}.")
                break
        removed.append(name)
    console.say()
    for name, (path, text, changed) in [*((name, stays[name]) for name in names if name not in removed),
                                        *((None, change) for change in changes),
                                        *((name, moves[name]) for name in removed)]:
        if changed == text:
            continue
        shown = path.relative_to(target).as_posix()
        with config.locked(path):
            check_unchanged(path, text)
            config.write_text_atomically(path, changed, newline="")
        if name in pins and name not in removed:
            console.say(f"{console.value(name)} stays on "
                        f"{console.value(where[name])}. "
                        f"Its {console.value('platform:')} settings are in "
                        f"{console.value(shown)}.")
        else:
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
    return 0 if removed == moving and not failed else 1


def deployed_word(name: str) -> str:
    return "yes" if config.is_deployed(name) else "no"


def move_app(name: str | None) -> int:
    """Move `name`, and the apps the user picks; None, in a project with no apps, moves the
    default platform in pdt.yml."""
    try:
        return ask_and_move(name)
    except NoAnswer:
        raise ConfigError(NO_TERMINAL) from None


def ask_and_move(name: str | None) -> int:
    if not deploy.can_prompt(None):
        raise ConfigError(NO_TERMINAL)
    marker = config.find_project() / PROJECT_FILE
    text = read_text(marker)
    root_cfg = config.load_yaml(marker)
    default = root_cfg.get("platform") or {}
    if name is None:
        platform = ask_place(default)
        if platform is None:
            console.say("Cancelled. Nothing changed.")
            return 0
        return apply(root_cfg, platform, platform, [],
                     [platform_change(marker, text, platform)], {})
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
                      [[str(number), other, deployed_word(other),
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
    if all("provider" in now[other] and same(now[other], platform)
           and same(deploy.resolved(config.merged_app(other)["platform"]), platform)
           for other in moving):
        console.say(f"{console.value(there)} is the current place. Nothing changed.")
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
    return apply(root_cfg, root_platform, platform, moving, changes, pins)
