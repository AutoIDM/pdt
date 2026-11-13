"""App profiles: the services pdt chooses for an app.

An app's `app_profile` key picks one. `job` (the default) runs on a schedule and
exits. `web` is an HTTP server behind a sign-in. The profile decides which config
keys the app may set and what each provider creates for it.
"""

from __future__ import annotations

from pdt.profiles import job, web

PROFILES = {"job": job, "web": web}
NAMES = tuple(PROFILES)
DEFAULT = "job"


def problems(app: dict, provider: str) -> list[str]:
    """The profile-specific config problems of a merged app, for `config.validate_app`."""
    name = app.get("app_profile") or DEFAULT
    if name not in NAMES:
        return [f"{app['name']}: app_profile must be one of: {', '.join(NAMES)}"]
    return PROFILES[name].problems(app, provider)
