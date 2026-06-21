"""Helpers for invoking host CLI tools (xdotool, xclip, ...).

Inside a Flatpak sandbox the app cannot run host binaries directly; it
reaches them via ``flatpak-spawn --host``. Both clipboard pasting and the
on-screen keyboard's key injection need the same dispatch logic, so it
lives here.
"""

from __future__ import annotations

import os
import shutil

_FLATPAK_MARKER = "/.flatpak-info"


def is_flatpak() -> bool:
    return os.path.exists(_FLATPAK_MARKER)


def command_candidates(tool: str) -> list[list[str]]:
    """Return executable command prefixes for a host helper tool.

    Tries the in-sandbox/in-PATH binary first, then ``flatpak-spawn --host``
    so a Flatpak build can fall back to the host's copy.
    """
    commands: list[list[str]] = []
    if shutil.which(tool):
        commands.append([tool])
    if is_flatpak() and shutil.which("flatpak-spawn"):
        commands.append(["flatpak-spawn", "--host", tool])
    return commands


def has_tool(tool: str) -> bool:
    return bool(command_candidates(tool))
