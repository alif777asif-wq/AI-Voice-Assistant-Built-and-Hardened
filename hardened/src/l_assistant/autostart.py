"""
"Start L when I log in" via the per-user Registry Run key
(HKEY_CURRENT_USER\\Software\\Microsoft\\Windows\\CurrentVersion\\Run) --
the standard mechanism for this that doesn't need admin rights (HKCU,
not HKLM, so no elevation prompt) and doesn't register a Windows
service (a service runs even with nobody logged in, under a system
account with no access to the interactive desktop session's audio
devices -- exactly wrong for a voice assistant that needs the logged-in
user's microphone and speakers). This writes one string value under the
CURRENT user's registry hive; nothing here touches machine-wide state.

Windows-only by construction -- `winreg` doesn't exist on other
platforms. Every public function still works when imported on Linux/
macOS (they raise a clear `RuntimeError` instead of failing on import
or with an opaque ModuleNotFoundError), which is why `winreg` is
imported lazily inside each function rather than at module load time --
`import l_assistant.autostart` must not itself require Windows, only
*calling* these functions does. Same pattern app_tray.py already uses
for pystray/PIL/tendo, for the same reason.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

_RUN_KEY_PATH = r"Software\Microsoft\Windows\CurrentVersion\Run"
_VALUE_NAME = "L Voice Assistant"


def is_available() -> bool:
    return sys.platform == "win32"


def _require_windows() -> None:
    if not is_available():
        raise RuntimeError(
            "Registry autostart is Windows-only (uses winreg, which "
            "doesn't exist on this platform)."
        )


def build_command_line(exe_path: Optional[Path] = None) -> str:
    """
    What actually launches at login. Always includes --tray, regardless
    of what tray.enabled happens to be set to in whatever config file is
    active at the time -- autostart existing to run invisibly in the
    background is the whole point of it, so the login-time launch
    shouldn't depend on a config file staying in whatever state it was
    in when this was enabled.

    Two cases:
    - A packaged build (`sys.frozen`, set by PyInstaller): `sys.executable`
      IS the built l_assistant.exe itself, so launch that directly.
    - Running from source: launch the CURRENT Python interpreter
      (sys.executable -- not an assumed global `python` on PATH, which
      may not exist or may resolve to a different interpreter/venv than
      the one L is actually installed in) with `-m l_assistant --tray`.
    """
    if exe_path is not None:
        return f'"{exe_path}" --tray'
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}" --tray'
    return f'"{sys.executable}" -m l_assistant --tray'


def is_enabled() -> bool:
    _require_windows()
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY_PATH, 0, winreg.KEY_READ) as key:
            winreg.QueryValueEx(key, _VALUE_NAME)
        return True
    except FileNotFoundError:
        return False


def enable(exe_path: Optional[Path] = None) -> None:
    _require_windows()
    import winreg

    command = build_command_line(exe_path)
    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, _RUN_KEY_PATH, 0, winreg.KEY_WRITE) as key:
        winreg.SetValueEx(key, _VALUE_NAME, 0, winreg.REG_SZ, command)


def disable() -> None:
    _require_windows()
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY_PATH, 0, winreg.KEY_WRITE) as key:
            winreg.DeleteValue(key, _VALUE_NAME)
    except FileNotFoundError:
        pass  # already disabled -- not an error, same end state either way


def toggle(exe_path: Optional[Path] = None) -> bool:
    """Flips the current state and returns the NEW state (True = now enabled)."""
    if is_enabled():
        disable()
        return False
    enable(exe_path)
    return True
