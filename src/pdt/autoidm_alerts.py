"""The autoidm alerts type: each event as JSON to the AutoIDM backend, signed by this computer.

An entry is `{type: autoidm}` plus its events, with no other key. `pdt register`
connects the computer; `pdt.utils.autoidm` holds the installation and signs each
request. A Windows task reads the installation files of the computer. A cloud
job has no such files, so it needs PDT_INSTALLATION_ID, PDT_INSTALLATION_KEY,
and PDT_API_URL in its environment.

This module is in deploy_common.LIFECYCLE_MODULES, so it imports only what
lifecycle.py's script header installs.
"""

from __future__ import annotations

KEYS: frozenset[str] = frozenset()
VERSION = 1


def body(event) -> dict:
    return {"version": VERSION, "event": {
        "name": event.name, "at": event.at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "params": event.params}}


def deliver(event, entry: dict, unhandled: bool) -> None:
    # Every script that loads config imports lifecycle, and only the scripts that send
    # events carry pyjwt and cryptography in their header.
    from pdt.utils import autoidm
    autoidm.post("/v1/events", body(event))
