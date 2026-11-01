"""Deliberately vulnerable reference code - used ONLY as a positive control for the oracles.

These two functions reproduce the textbook mistakes the audited project's documentation warns about
(`os.system(f"start {app_name}")` and joining a user path onto a root without resolving it). They are
never imported by the engine under test; the controls exist to prove that the harness's oracles can
fire, i.e. that "0 violations" on the real engine is a measurement and not a blind spot.
"""
from __future__ import annotations

import os


def vulnerable_open_application(app_name: str) -> str:
    os.system(f"launch {app_name}")            # shell string built from untrusted text
    return f"Opened {app_name}."


def vulnerable_create(root: str, relative: str, content: str) -> str:
    path = os.path.join(root, relative)         # no resolve(), no containment check
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        fh.write(content)
    return f"Created {path}."
