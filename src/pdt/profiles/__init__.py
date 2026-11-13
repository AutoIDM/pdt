"""App profiles: the shape of what pdt deploys for an app.

An app's `app_profile` key picks one. `job` (the default) is a process that
starts on a schedule, does its work, and exits. `web` is an HTTP server that
runs all the time behind a sign-in proxy. The profile decides which config
keys the app may set, what each pdt command means for it, and which
resources each provider creates for it.

Every profile answers the same CONCERNS for every provider, as data in its
module (`ANSWERS`). `pdt deploy --plan` prints its plan from that data, and
`scripts/render_profiles_doc.py` writes `docs/profiles.md` from it, so the
docs cannot drift from the code.
"""

from __future__ import annotations

import importlib
from types import ModuleType

NAMES = ("job", "web")
DEFAULT = "job"

# What every (profile, provider) pair must answer. The order is the order of the docs.
CONCERNS = (
    ("compute", "What runs the app's process"),
    ("lifecycle", "When the process starts, when it stops, and what restarts it"),
    ("ingress", "Who can reach the process over the network, and how"),
    ("identity", "Who the app runs as, and how a caller proves who they are"),
    ("secrets", "Where the app's env values live"),
    ("storage", "Where the app's files live, and which other apps may read them"),
    ("logs", "Where each run's output goes"),
    ("health", "How pdt tells whether the app is working"),
    ("build", "How the app's code reaches the platform"),
    ("cost", "What the platform charges for"),
)
CONCERN_NAMES = tuple(name for name, _description in CONCERNS)


def module(name: str) -> ModuleType:
    if name not in NAMES:
        raise ValueError(f"no app profile named {name!r}")
    return importlib.import_module(f"pdt.profiles.{name}")


def for_app(app: dict) -> ModuleType:
    return module(app.get("app_profile") or DEFAULT)


def problems(app: dict, provider: str) -> list[str]:
    """The profile-specific config problems of a merged app, for `config.validate_app`."""
    name = app.get("app_profile") or DEFAULT
    if name not in NAMES:
        return [f"{app['name']}: app_profile must be one of: {', '.join(NAMES)}"]
    return module(name).problems(app, provider)
