"""
OS-level automation tools the LLM can invoke by name: launching/closing
applications, adjusting system volume, minimizing windows, browser tab
control, web search, and file operations.

Every function here is written for one hard constraint the source
blueprint this was built from didn't account for: the caller is an LLM
interpreting voice-transcribed text, not a developer typing a CLI
command. That input path -- microphone -> STT -> LLM tool-call arguments
-- is unusually easy to get wrong (mishearing, a hallucinated argument,
an over-eager tool choice), so two properties hold for every function
below regardless of what the source blueprint specified:

1. No function ever builds a shell command string. Every subprocess call
   uses the list form with shell=False, so there is no command-injection
   surface no matter what text an LLM or a mis-transcription puts into an
   argument. (The blueprint's `os.system(f"start {app_name}")` and
   `os.system(f"taskkill /f /im {app_name}.exe")` are the textbook
   pattern this avoids -- an app_name of e.g. `"notepad & del /f /q C:\\"`
   would execute as two commands under `os.system`; under
   `subprocess.Popen(["notepad & del /f /q C:\\"], shell=False)` it is
   just one, nonexistent, program name that fails to launch.)

2. Anything that destroys or overwrites existing data -- `file_operation`
   with action="delete" or "rename", or any action whose target path
   already exists -- is refused unless a matching confirmation was
   already given. See `ConfirmationRequired` below; the orchestrator
   turns this into a spoken "are you sure?" / "say yes to confirm"
   exchange before ever calling shutil.rmtree or Path.rename. This
   defaults on and is controlled by
   `tools.require_confirmation_for_destructive` in config.

Every function that touches the filesystem additionally refuses to
operate outside `allowed_roots` (resolved to the user's home directory if
left empty in config), checked by resolving the target path -- following
symlinks -- and confirming it lands inside one of the allowed roots. That
stops both a wildly wrong LLM-hallucinated path and a deliberately
crafted path-traversal argument from ever reaching shutil/Path.
"""

from __future__ import annotations

import ipaddress
import logging
import os
import re
import shutil
import subprocess
import sys
import urllib.parse
import webbrowser
from pathlib import Path
from typing import Dict, List, Optional

from l_assistant.memory.user_profile import UserProfileStore

logger = logging.getLogger(__name__)

# Characters with no legitimate place in an application/process name on
# either OS. Rejected outright even though list-form subprocess calls
# (shell=False) already prevent shell interpretation -- this is defense
# in depth, not the only line of defense.
_UNSAFE_NAME_CHARS = set(';&|`$<>"\'\n\r')

_SUBPROCESS_TIMEOUT_S = 10

# Key-name -> platform-specific identifier, shared by every method that
# needs to send a keyboard shortcut to whatever window currently has OS
# focus (manage_browser_tabs' close/next/previous, minimize_all_windows
# on Windows). This is the same fundamental mechanism -- and the same
# fundamental limitation, focus-dependence, there is no way around that
# without deeper browser-specific automation -- on every platform; only
# the OS-level API differs.
_WIN_VK_CODES = {
    "ctrl": 0x11,
    "shift": 0x10,
    "super": 0x5B,
    "tab": 0x09,
    "t": 0x54,
    "w": 0x57,
    "d": 0x44,
    "m": 0x4D,
}
_XDOTOOL_KEY_NAMES = {
    "ctrl": "ctrl",
    "shift": "shift",
    "super": "super",
    "tab": "Tab",
    "t": "t",
    "w": "w",
    "d": "d",
    "m": "m",
}

# Shared by control_window's Windows and Linux branches so both produce
# the same wording for the same action.
_WINDOW_ACTION_PAST_TENSE = {
    "minimize": "Minimized",
    "maximize": "Maximized",
    "restore": "Restored",
    "focus": "Focused",
}


class ToolError(RuntimeError):
    """An expected, explainable tool failure -- caught by ToolDispatcher
    and turned into a result string the model relays to the user, rather
    than crashing the turn."""


class ConfirmationRequired(RuntimeError):
    """
    Raised instead of executing a destructive file_operation. Carries
    what would happen; the orchestrator speaks `message`, waits for the
    user's next utterance, and -- only on a clear affirmative -- re-calls
    file_operation with the same action/source/destination plus
    `confirmed=True`.
    """

    def __init__(self, message: str, action: str, source: str, destination: str = "") -> None:
        super().__init__(message)
        self.action = action
        self.source = source
        self.destination = destination


def _validate_name(value: str, field: str) -> str:
    value = (value or "").strip()
    if not value:
        raise ToolError(f"'{field}' was empty.")
    if any(ch in _UNSAFE_NAME_CHARS for ch in value):
        raise ToolError(
            f"'{field}' ({value!r}) contains characters that aren't valid in "
            f"an application or process name; refusing to run it."
        )
    return value


# ---- Hardening helpers (every rule below is covered by tests/test_hardening.py
# and by the adversarial harness in the repository that ships this patch) ------

# A bare program / process name: starts with a letter or digit, then letters, digits,
# space and . _ + @ - only. No path separators, no drive letters, no leading '-', no
# URL syntax, no wildcard or regular-expression operators except the literal '.' and '+'
# (which close_application escapes before they reach pkill).
_PROGRAM_NAME_RE = re.compile(r"^[^\W_][\w .+@-]*$")


def _validate_program_name(value: str, field: str) -> str:
    value = _validate_name(value, field)
    if not _PROGRAM_NAME_RE.match(value):
        raise ToolError(
            f"'{field}' ({value!r}) isn't a plain application name (no paths, options, "
            f"URLs or wildcards are accepted); refusing to run it."
        )
    return value


def _escape_ere(text: str) -> str:
    """Escape POSIX extended-regex operators so pkill treats `text` literally."""
    return re.sub(r"([\\.^$*+?()\[\]{}|])", r"\\\1", text)


_WEB_SCHEMES = ("http", "https")
_HOST_LABEL_RE = re.compile(r"^[A-Za-z0-9\u00a1-\uffff](?:[A-Za-z0-9\u00a1-\uffff-]{0,61}[A-Za-z0-9\u00a1-\uffff])?$")


def _validated_web_url(url: str) -> str:
    """Only well-formed http(s) addresses may reach the browser: never another scheme
    (file:, javascript:, smb: ...), never something a browser would parse as an option."""
    if any(ord(ch) < 33 or ord(ch) == 127 for ch in url):
        raise ToolError("That address contains spaces or control characters; refusing to open it.")
    if url.startswith("-"):
        raise ToolError("That address looks like a command-line option; refusing to open it.")
    try:
        parts = urllib.parse.urlsplit(url)
        host = parts.hostname
        parts.port  # noqa: B018 -- raises ValueError for a malformed port
    except ValueError:
        raise ToolError("That address is malformed; refusing to open it.")
    if parts.scheme.lower() not in _WEB_SCHEMES:
        raise ToolError(
            f"Only http and https addresses can be opened (got {parts.scheme or 'no scheme'!r}); refusing."
        )
    if not host:
        raise ToolError("That address has no host name; refusing to open it.")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        if not all(_HOST_LABEL_RE.match(label) for label in host.rstrip(".").split(".")):
            raise ToolError("That address has an invalid host name; refusing to open it.")
    return url


# Places inside an allowed root where a file written by the assistant is later executed or
# trusted without anyone looking at it (shell/session startup, SSH, credentials, autostart).
_PROTECTED_FILE_NAMES = {
    ".bashrc", ".bash_profile", ".bash_login", ".bash_logout", ".profile", ".zshrc", ".zshenv",
    ".zprofile", ".zlogin", ".xprofile", ".xinitrc", ".xsession", "authorized_keys", "authorized_keys2",
    ".netrc", ".git-credentials", ".npmrc", ".pypirc", ".pgpass",
}
_PROTECTED_DIR_SEQUENCES = [
    ((".ssh",), "SSH keys and configuration"),
    ((".gnupg",), "GPG keys"),
    ((".aws",), "cloud credentials"),
    ((".kube",), "cluster credentials"),
    ((".config", "autostart"), "desktop autostart entries"),
    ((".config", "systemd"), "systemd user units"),
    (("start menu", "programs", "startup"), "the Windows startup folder"),
    (("library", "launchagents"), "macOS launch agents"),
    ((".git", "hooks"), "Git hooks"),
]


def _protected_reason(*paths: Path) -> Optional[str]:
    """Why any of `paths` (lexical and symlink-resolved forms) is a protected location, if it is."""
    for path in paths:
        parts = [part.lower() for part in path.parts]
        if parts and parts[-1] in _PROTECTED_FILE_NAMES:
            return "a shell-startup, SSH or credential file"
        for sequence, reason in _PROTECTED_DIR_SEQUENCES:
            width = len(sequence)
            if any(tuple(parts[i : i + width]) == sequence for i in range(len(parts) - width + 1)):
                return reason
    return None


def _default_allowed_roots() -> List[Path]:
    return [Path.home().resolve()]


def _resolve_allowed_roots(allowed_roots: Optional[List[str]]) -> List[Path]:
    if not allowed_roots:
        return _default_allowed_roots()
    return [Path(r).expanduser().resolve() for r in allowed_roots]


def _check_within_allowed_roots(path: Path, roots: List[Path]) -> None:
    resolved = path.expanduser().resolve()
    for root in roots:
        try:
            resolved.relative_to(root)
            return
        except ValueError:
            continue
    roots_str = ", ".join(str(r) for r in roots)
    raise ToolError(
        f"'{resolved}' is outside the allowed director"
        f"{'y' if len(roots) == 1 else 'ies'} ({roots_str}); refusing. "
        f"Add it to tools.allowed_roots in config if this should be permitted."
    )


def _same_file(a: Path, b: Path) -> bool:
    try:
        return os.path.samefile(a, b)
    except OSError:
        return False


class SystemToolsEngine:
    """Executes OS-level automation: apps, volume, files, browser, search."""

    def __init__(
        self,
        allowed_roots: Optional[List[str]] = None,
        require_confirmation: bool = True,
        command_execution_enabled: bool = False,
        power_management_enabled: bool = False,
        power_min_delay_seconds: int = 15,
        user_profile: Optional[UserProfileStore] = None,
        protect_sensitive_paths: bool = True,
    ) -> None:
        self._allowed_roots = _resolve_allowed_roots(allowed_roots)
        # Refuse to create/copy/rename into or out of startup, SSH and credential locations
        # (see _protected_reason). On by default; set False only if you really want the
        # assistant managing those files.
        self._protect_sensitive_paths = protect_sensitive_paths
        self._require_confirmation = require_confirmation
        # Both default OFF even when the rest of tools.* is on -- see
        # sections 6/7 below for why these two are gated separately from
        # everything else in this class rather than folded into the same
        # tools.enabled switch as open_application/file_operation/etc.
        self._command_execution_enabled = command_execution_enabled
        self._power_management_enabled = power_management_enabled
        self._power_min_delay_seconds = max(5, power_min_delay_seconds)
        # None (the default) means "no persistent profile configured" --
        # remember_user_fact/forget_user_fact raise ToolError rather
        # than silently no-opping, same as every other not-configured
        # capability in this class (command_execution, power_management).
        self._user_profile = user_profile

    # ---- 1. Application / window management --------------------------

    def open_application(self, app_name: str) -> str:
        name = _validate_program_name(app_name, "app_name")
        try:
            if sys.platform == "win32":
                # The safe, shell-free Windows API for "launch this by
                # name/path" -- no string is ever handed to a shell.
                os.startfile(name)  # type: ignore[attr-defined]
            else:
                subprocess.Popen(
                    [name],
                    shell=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            return f"Opened {name}."
        except FileNotFoundError:
            raise ToolError(f"Couldn't find an application named '{name}'.")
        except OSError as exc:
            raise ToolError(f"Failed to open '{name}': {exc}")

    def close_application(self, app_name: str) -> str:
        name = _validate_program_name(app_name, "app_name")
        if name.lower().endswith(".exe"):
            name = name[: -len(".exe")]
        try:
            if sys.platform == "win32":
                result = subprocess.run(
                    ["taskkill", "/f", "/im", f"{name}.exe"],
                    shell=False,
                    capture_output=True,
                    text=True,
                    timeout=_SUBPROCESS_TIMEOUT_S,
                )
            else:
                result = subprocess.run(
                    # pkill's pattern is a regular expression: escape it so "." or ".*" in a
                    # name can never match processes other than the one named.
                    ["pkill", "-x", "--", _escape_ere(name)],
                    shell=False,
                    capture_output=True,
                    text=True,
                    timeout=_SUBPROCESS_TIMEOUT_S,
                )
            if result.returncode != 0:
                return f"No running process matched '{name}'."
            return f"Closed {name}."
        except subprocess.TimeoutExpired:
            raise ToolError(f"Timed out trying to close '{name}'.")
        except OSError as exc:
            raise ToolError(f"Failed to close '{name}': {exc}")

    def minimize_all_windows(self) -> str:
        if sys.platform == "win32":
            self._send_key_combo_windows(["super", "d"])
            return "Minimized all windows."

        if sys.platform == "darwin":
            raise ToolError(
                "Minimizing all windows isn't implemented on macOS -- unlike "
                "Windows (Win+D) or Linux (the EWMH 'show desktop' protocol "
                "every modern window manager implements), there's no single "
                "reliable scriptable equivalent across macOS window managers."
            )

        # Linux: EWMH's "show desktop" state, toggled via wmctrl -- this
        # talks to whichever window manager is running (Cinnamon's Muffin,
        # GNOME's Mutter, KDE's KWin, ...) through the standard protocol,
        # so it works the same way regardless of desktop environment,
        # unlike a simulated keystroke, which would need a different
        # keybinding guessed per desktop environment.
        try:
            result = subprocess.run(
                ["wmctrl", "-k", "on"],
                shell=False,
                capture_output=True,
                text=True,
                timeout=_SUBPROCESS_TIMEOUT_S,
            )
        except FileNotFoundError:
            raise ToolError(
                "This needs 'wmctrl', which isn't installed. Install it with: "
                "sudo apt-get install wmctrl -- then try again."
            )
        except subprocess.TimeoutExpired:
            raise ToolError("Timed out minimizing windows.")
        if result.returncode != 0:
            raise ToolError(f"wmctrl failed: {result.stderr.strip() or 'unknown error'}")
        return "Minimized all windows."

    # -- shared keyboard-shortcut dispatch, used above and by
    # manage_browser_tabs' close/next/previous actions -----------------

    def _send_key_combo_windows(self, keys: List[str]) -> None:
        import ctypes

        user32 = ctypes.windll.user32  # type: ignore[attr-defined]
        keyup = 0x2
        codes = [_WIN_VK_CODES[k] for k in keys]
        for code in codes:
            user32.keybd_event(code, 0, 0, 0)
        for code in reversed(codes):
            user32.keybd_event(code, 0, keyup, 0)

    def _send_key_combo_linux(self, keys: List[str]) -> None:
        combo = "+".join(_XDOTOOL_KEY_NAMES[k] for k in keys)
        try:
            result = subprocess.run(
                ["xdotool", "key", combo],
                shell=False,
                capture_output=True,
                text=True,
                timeout=_SUBPROCESS_TIMEOUT_S,
            )
        except FileNotFoundError:
            raise ToolError(
                "This needs 'xdotool', which isn't installed. Install it "
                "with: sudo apt-get install xdotool -- then try again. "
                "(Needs an X11 session -- Linux Mint Cinnamon uses X11 by "
                "default, so this should just work once installed.)"
            )
        except subprocess.TimeoutExpired:
            raise ToolError("Timed out sending the keystroke.")
        if result.returncode != 0:
            raise ToolError(f"xdotool failed: {result.stderr.strip() or 'unknown error'}")

    def _send_key_combo_macos(self, keystroke: str, modifiers: List[str]) -> None:
        # Best-effort: macOS isn't a target platform for this project
        # (Linux Mint + Windows 11 -- see README), so this is included for
        # completeness but has not been run on real macOS hardware.
        mods = ", ".join(f"{m} down" for m in modifiers)
        script = f'tell application "System Events" to keystroke "{keystroke}"'
        if mods:
            script += f" using {{{mods}}}"
        try:
            result = subprocess.run(
                ["osascript", "-e", script],
                shell=False,
                capture_output=True,
                text=True,
                timeout=_SUBPROCESS_TIMEOUT_S,
            )
        except FileNotFoundError:
            raise ToolError("osascript is unavailable -- unexpected on macOS.")
        except subprocess.TimeoutExpired:
            raise ToolError("Timed out sending the keystroke.")
        if result.returncode != 0:
            raise ToolError(f"osascript failed: {result.stderr.strip() or 'unknown error'}")

    # ---- 2. Volume -----------------------------------------------------

    def adjust_volume(self, action: str, percent: int = 10) -> str:
        action = (action or "").strip().lower()
        if action not in ("up", "down", "mute"):
            raise ToolError(f"Unknown volume action '{action}'.")
        percent = max(1, min(100, int(percent)))
        try:
            if sys.platform == "win32":
                return self._adjust_volume_windows(action, percent)
            if sys.platform == "darwin":
                return self._adjust_volume_macos(action, percent)
            return self._adjust_volume_linux(action, percent)
        except subprocess.TimeoutExpired:
            raise ToolError("Timed out adjusting volume.")
        except OSError as exc:
            raise ToolError(f"Failed to adjust volume: {exc}")

    def _adjust_volume_windows(self, action: str, percent: int) -> str:
        # Fixed keycodes chosen from a closed set above -- the only
        # "variable" in this command is which of three constants to use,
        # never user-controlled text.
        key_map = {"up": "{VOLUME_UP}", "down": "{VOLUME_DOWN}", "mute": "{VOLUME_MUTE}"}
        presses = 1 if action == "mute" else max(1, percent // 2)
        script = (
            "$w = New-Object -ComObject WScript.Shell; "
            f"1..{presses} | ForEach-Object {{ $w.SendKeys('{key_map[action]}') }}"
        )
        subprocess.run(
            ["powershell", "-NoProfile", "-Command", script],
            shell=False,
            capture_output=True,
            timeout=_SUBPROCESS_TIMEOUT_S,
        )
        return f"Volume {action}."

    def _adjust_volume_linux(self, action: str, percent: int) -> str:
        if action == "mute":
            args = ["amixer", "-D", "pulse", "sset", "Master", "toggle"]
        else:
            sign = "+" if action == "up" else "-"
            args = ["amixer", "-D", "pulse", "sset", "Master", f"{percent}%{sign}"]
        subprocess.run(args, shell=False, capture_output=True, timeout=_SUBPROCESS_TIMEOUT_S)
        return f"Volume {action}."

    def _adjust_volume_macos(self, action: str, percent: int) -> str:
        if action == "mute":
            script = "set volume output muted true"
        else:
            delta = percent if action == "up" else -percent
            script = f"set volume output volume ((output volume of (get volume settings)) + {delta})"
        subprocess.run(
            ["osascript", "-e", script], shell=False, capture_output=True, timeout=_SUBPROCESS_TIMEOUT_S
        )
        return f"Volume {action}."

    # ---- 3. File operations ---------------------------------------------

    def file_operation(
        self,
        action: str,
        source: str,
        destination: str = "",
        content: str = "",
        confirmed: bool = False,
    ) -> str:
        action = (action or "").strip().lower()
        if action not in ("create", "copy", "rename", "delete", "list"):
            raise ToolError(f"Unknown file action '{action}'.")
        if not source:
            raise ToolError("file_operation requires 'source'.")

        src = Path(source).expanduser().resolve()
        _check_within_allowed_roots(src, self._allowed_roots)
        if action != "list":
            self._refuse_protected(source, src)

        if action == "list":
            # The only read-only action here, and deliberately the only
            # way to resolve an ambiguous reference ("the folder on my
            # desktop", no name given) into a real path: this returns
            # actual directory contents, not a guess. Calling
            # file_operation with a name pulled from nowhere would be
            # worse than asking a clarifying question; looking first and
            # confirming there's genuinely one matching item is
            # categorically different from guessing, and is what the
            # system prompt instructs the model to do before falling
            # back to asking. Doesn't touch or bypass confirmation --
            # a subsequent delete/rename/copy of whatever this finds
            # still goes through the exact same ConfirmationRequired
            # gate below, unaffected by how the path was determined.
            if not src.exists():
                raise ToolError(f"'{src}' doesn't exist.")
            if not src.is_dir():
                raise ToolError(f"'{src}' is a file, not a folder -- nothing to list.")
            try:
                entries = sorted(src.iterdir(), key=lambda p: p.name.lower())
            except PermissionError:
                raise ToolError(f"No permission to list '{src}'.")
            folders = [p.name for p in entries if p.is_dir()]
            files = [p.name for p in entries if not p.is_dir()]
            if not folders and not files:
                return f"'{src}' is empty."
            parts = []
            if folders:
                parts.append(f"{len(folders)} folder(s): {', '.join(folders)}")
            if files:
                parts.append(f"{len(files)} file(s): {', '.join(files)}")
            return f"Contents of '{src}': " + "; ".join(parts)

        if action == "create":
            if src.exists() and confirmed is not True and self._require_confirmation:
                raise ConfirmationRequired(
                    f"'{src.name}' already exists. Overwrite it?",
                    action="create",
                    source=str(src),
                )
            src.parent.mkdir(parents=True, exist_ok=True)
            src.write_text(content, encoding="utf-8")
            return f"Created {src}."

        if action == "copy":
            if not destination:
                raise ToolError("file_operation 'copy' requires 'destination'.")
            dst = Path(destination).expanduser().resolve()
            _check_within_allowed_roots(dst, self._allowed_roots)
            self._refuse_protected(destination, dst)
            if dst.exists() and confirmed is not True and self._require_confirmation:
                raise ConfirmationRequired(
                    f"'{dst.name}' already exists at the destination. Overwrite it?",
                    action="copy",
                    source=str(src),
                    destination=str(dst),
                )
            if not src.exists():
                raise ToolError(f"'{src}' doesn't exist.")
            dst.parent.mkdir(parents=True, exist_ok=True)
            if src.is_dir():
                shutil.copytree(src, dst, dirs_exist_ok=True)
            else:
                shutil.copy2(src, dst)
            return f"Copied {src} to {dst}."

        if action == "rename":
            if not destination:
                raise ToolError("file_operation 'rename' requires 'destination'.")
            dst = Path(destination).expanduser().resolve()
            _check_within_allowed_roots(dst, self._allowed_roots)
            self._refuse_protected(destination, dst)
            if not src.exists():
                raise ToolError(f"'{src}' doesn't exist.")
            if self._is_allowed_root(src) or self._is_allowed_root(dst):
                raise ToolError("Refusing to rename or replace an allowed root folder itself.")
            replaces = os.path.lexists(dst) and not _same_file(src, dst)
            if confirmed is not True and self._require_confirmation:
                if replaces:
                    raise ConfirmationRequired(
                        f"'{dst.name}' already exists. Replace it with '{src.name}'? "
                        f"The existing '{dst.name}' goes to the trash first, so it can be recovered.",
                        action="rename",
                        source=str(src),
                        destination=str(dst),
                    )
                raise ConfirmationRequired(
                    f"Rename '{src.name}' to '{dst.name}'?",
                    action="rename",
                    source=str(src),
                    destination=str(dst),
                )
            if replaces:
                # Path.rename silently destroys an existing target on POSIX; make it recoverable.
                self._move_to_trash(dst)
            src.rename(dst)
            return f"Renamed {src.name} to {dst.name}."

        # action == "delete"
        if not src.exists():
            raise ToolError(f"'{src}' doesn't exist.")
        if self._is_allowed_root(src):
            raise ToolError("Refusing to delete an allowed root folder itself.")
        if confirmed is not True and self._require_confirmation:
            kind = "folder" if src.is_dir() else "file"
            raise ConfirmationRequired(
                f"Delete the {kind} '{src.name}'? It'll go to the trash, "
                f"so it's recoverable from there if needed.",
                action="delete",
                source=str(src),
            )
        # send2trash, not shutil.rmtree/Path.unlink: an LLM interpreting a
        # voice transcript deciding what counts as "delete this" is exactly
        # the kind of judgment call worth making recoverable-by-default,
        # independent of the confirmation step above -- confirmation guards
        # against acting on a misheard command at all, this guards against
        # a *correctly heard* delete turning out to be the wrong file.
        # Same allowed_roots/confirmation gate either way; the only change
        # is where the bytes end up.
        self._move_to_trash(src)
        return f"Moved {src} to the trash."

    @staticmethod
    def _move_to_trash(path: Path) -> None:
        try:
            from send2trash import send2trash

            send2trash(str(path))
        except ImportError:
            raise ToolError(
                "The 'send2trash' package isn't installed, so delete is "
                "refused rather than silently falling back to a permanent "
                "shutil delete. Install it with 'pip install send2trash' "
                "(already in requirements.txt)."
            )
        except Exception as exc:
            raise ToolError(f"Couldn't move '{path}' to the trash: {exc}")

    def _is_allowed_root(self, path: Path) -> bool:
        return any(path == root for root in self._allowed_roots)

    def _refuse_protected(self, raw: str, resolved: Path) -> None:
        if not self._protect_sensitive_paths:
            return
        lexical = Path(os.path.normpath(os.path.join(os.getcwd(), os.path.expanduser(raw))))
        reason = _protected_reason(lexical, resolved)
        if reason:
            raise ToolError(
                f"'{resolved.name}' is in a protected location ({reason}); refusing to read, create "
                f"or change it by voice. Construct SystemToolsEngine(protect_sensitive_paths=False) "
                f"if the assistant really should manage those files."
            )

    # ---- 4. Web search & browser tabs ------------------------------------

    # Curated, easy to extend -- add an entry here for any site you want
    # a guaranteed-correct direct hit on rather than relying on the
    # slug-guessing fallback in open_website below.
    _KNOWN_SITE_URLS: Dict[str, str] = {
        "google": "https://www.google.com",
        "youtube": "https://www.youtube.com",
        "facebook": "https://www.facebook.com",
        "whatsapp": "https://web.whatsapp.com",
        "instagram": "https://www.instagram.com",
        "twitter": "https://x.com",
        "x": "https://x.com",
        "reddit": "https://www.reddit.com",
        "gmail": "https://mail.google.com",
        "github": "https://github.com",
        "claude": "https://claude.ai",
        "claude ai": "https://claude.ai",
        "chatgpt": "https://chat.openai.com",
        "gemini": "https://gemini.google.com",
        "gemini ai": "https://gemini.google.com",
        "netflix": "https://www.netflix.com",
        "spotify": "https://open.spotify.com",
        "amazon": "https://www.amazon.com",
        "linkedin": "https://www.linkedin.com",
        "discord": "https://discord.com/app",
        "wikipedia": "https://www.wikipedia.org",
    }

    def open_website(self, target: str) -> str:
        """
        "Open Facebook" / "open whatsapp" / "open example.com" / "open
        [a site not in the curated list above]" -- direct navigation,
        no query. Distinct from search_web below, which always performs
        a search (even a site-scoped one via `site:`) and therefore
        needs a query string; asking search_web to "search for
        Facebook" is not the same request as "take me to Facebook", and
        before this tool existed there was no way to express the second
        one without it silently becoming the first.
        """
        target = (target or "").strip()
        if not target:
            raise ToolError("open_website requires a non-empty target.")

        key = target.lower()
        url = self._KNOWN_SITE_URLS.get(key)

        if url is None:
            if "." in target and " " not in target:
                # Already domain/URL-shaped ("example.com",
                # "https://foo.bar/baz") -- use it as-is rather than
                # running it through the slug-guessing path below.
                url = target if "://" in target else f"https://{target}"
            else:
                # Not in the curated list, not already domain-shaped --
                # best-effort guess rather than falling back to a Google
                # search, which is exactly the behavior this tool exists
                # to avoid. Right often enough for a plain single-word
                # product/brand name to be worth trying first; a wrong
                # guess just doesn't resolve to what they wanted, same
                # failure mode as a human mistyping a URL, not a
                # dangerous one -- see ToolError/ConfirmationRequired
                # usage elsewhere in this class for what actually
                # warrants gating, which opening a browser tab doesn't.
                slug = re.sub(r"[^a-z0-9]", "", key)
                if not slug:
                    raise ToolError(f"Can't tell what site '{target}' refers to.")
                url = f"https://{slug}.com"

        webbrowser.open(_validated_web_url(url))
        return f"Opening {target}."

    _SITE_SEARCH_URL_TEMPLATES: Dict[str, str] = {
        "google": "https://www.google.com/search?q={q}",
        "youtube": "https://www.youtube.com/results?search_query={q}",
    }

    def search_web(self, query: str, site: str = "google") -> str:
        """
        Replaces the earlier `google_search`-only tool. That version
        always searched Google regardless of what the user asked for --
        "search X on YouTube" produced a Google search for "X YouTube"
        instead of an actual YouTube search, because there was no way to
        express "which site" as anything other than extra words stuffed
        into the query string. `site` makes that an explicit, separate
        argument instead.
        """
        query = (query or "").strip()
        if not query:
            raise ToolError("search_web requires a non-empty query.")

        site_key = (site or "google").strip().lower()
        template = self._SITE_SEARCH_URL_TEMPLATES.get(site_key)
        if template is not None:
            url = template.format(q=urllib.parse.quote(query))
            target = site_key.capitalize()
        else:
            # Unrecognized site name -- fall back to a Google search
            # scoped to that site via the `site:` operator rather than
            # silently dropping the request, so an unlisted site (e.g.
            # "reddit") still gets *some* attempt at targeting instead of
            # reverting to the exact bug this tool exists to fix.
            scoped_query = f"{query} site:{site_key}"
            url = self._SITE_SEARCH_URL_TEMPLATES["google"].format(
                q=urllib.parse.quote(scoped_query)
            )
            target = f"Google, scoped to {site_key}"

        webbrowser.open(_validated_web_url(url))
        return f"Searching {target} for: {query}"

    _BROWSER_TAB_KEY_COMBOS: Dict[str, List[str]] = {
        "new": ["ctrl", "t"],
        "close": ["ctrl", "w"],
        "next": ["ctrl", "tab"],
        "previous": ["ctrl", "shift", "tab"],
    }
    # AppleScript's `keystroke` command types literal characters -- that
    # works for "t"/"w" below but would type the literal string "tab"
    # into whatever has focus for "next"/"previous" rather than pressing
    # the Tab key, which needs a numeric `key code` this project's shared
    # macOS dispatch doesn't send. Since macOS isn't a target platform
    # here (Linux Mint + Windows 11 -- see README), these two fail
    # clearly instead of silently typing the wrong thing.
    _MACOS_UNSUPPORTED_TAB_ACTIONS = {"next", "previous"}

    def manage_browser_tabs(self, action: str) -> str:
        action = (action or "").strip().lower()
        combo = self._BROWSER_TAB_KEY_COMBOS.get(action)
        if combo is None:
            raise ToolError(
                f"Unknown browser tab action '{action}'. Valid actions: "
                f"{sorted(self._BROWSER_TAB_KEY_COMBOS)}."
            )

        if sys.platform == "win32":
            self._send_key_combo_windows(combo)
        elif sys.platform == "darwin":
            if action in self._MACOS_UNSUPPORTED_TAB_ACTIONS:
                raise ToolError(
                    f"Switching tabs isn't implemented on macOS -- 'new' "
                    f"and 'close' work, '{action}' doesn't."
                )
            mac_combo = ["cmd" if k == "ctrl" else k for k in combo]
            *modifiers, keystroke = mac_combo
            self._send_key_combo_macos(keystroke, modifiers)
        else:
            self._send_key_combo_linux(combo)

        return f"Browser tab: {action}."

    # ---- 5. Per-window control --------------------------------------------
    #
    # minimize_all_windows (section 1) is unconditional and needed no
    # separate enable flag; this is the same risk class -- it changes
    # window *state*, nothing persistent, nothing destructive -- so it's
    # gated by tools.enabled like everything else in this class, not by
    # the two stricter flags sections 6 and 7 introduce below.

    def control_window(self, action: str, target: str = "") -> str:
        """
        `target` is matched case-insensitively as a substring of the
        window title (e.g. "brave" matches "Brave Browser - Reddit").
        Leave it empty to act on whichever window currently has OS focus.
        If more than one open window matches, the first one found is
        used -- there's no voice-friendly way to disambiguate further
        without a lot of extra machinery, so this optimizes for the
        common case (one match) over handling the rare one gracefully.
        """
        action = (action or "").strip().lower()
        if action not in _WINDOW_ACTION_PAST_TENSE:
            raise ToolError(
                f"Unknown window action '{action}'. Valid actions: "
                f"{sorted(_WINDOW_ACTION_PAST_TENSE)}."
            )

        if sys.platform == "win32":
            return self._control_window_windows(action, target.strip())
        if sys.platform == "darwin":
            raise ToolError(
                "Per-window control isn't implemented on macOS -- not a "
                "target platform for this project (Linux Mint + Windows "
                "11 -- see README)."
            )
        return self._control_window_linux(action, target.strip())

    def _find_window_windows(self, target: str):
        """Returns (hwnd, title). Written against the documented Win32
        API surface; not run against a real Windows session -- see the
        focus-stealing note in _control_window_windows below for the one
        part of this most likely to need a follow-up once you do."""
        import ctypes

        user32 = ctypes.windll.user32  # type: ignore[attr-defined]

        if not target:
            hwnd = user32.GetForegroundWindow()
            if not hwnd:
                raise ToolError("Couldn't determine the active window.")
            length = user32.GetWindowTextLengthW(hwnd)
            buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buf, length + 1)
            return hwnd, buf.value

        matches: List[tuple] = []
        enum_proc_type = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

        def _callback(hwnd, _lparam):
            if not user32.IsWindowVisible(hwnd):
                return True
            length = user32.GetWindowTextLengthW(hwnd)
            if length == 0:
                return True
            buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buf, length + 1)
            if target.lower() in buf.value.lower():
                matches.append((hwnd, buf.value))
            return True

        user32.EnumWindows(enum_proc_type(_callback), 0)
        if not matches:
            raise ToolError(f"No open window matching '{target}'.")
        return matches[0]

    def _control_window_windows(self, action: str, target: str) -> str:
        import ctypes

        hwnd, title = self._find_window_windows(target)
        user32 = ctypes.windll.user32  # type: ignore[attr-defined]
        sw_restore, sw_minimize, sw_maximize = 9, 6, 3
        if action == "minimize":
            user32.ShowWindow(hwnd, sw_minimize)
        elif action == "maximize":
            user32.ShowWindow(hwnd, sw_maximize)
        elif action == "restore":
            user32.ShowWindow(hwnd, sw_restore)
        else:  # focus
            user32.ShowWindow(hwnd, sw_restore)
            # Windows can silently refuse SetForegroundWindow if this
            # process wasn't the one that last held focus -- a documented
            # OS-level restriction, not a bug here. If it doesn't reliably
            # steal focus in practice, the standard fix is
            # AttachThreadInput with the current foreground thread's ID;
            # worth adding only once you've actually hit the limitation.
            user32.SetForegroundWindow(hwnd)
        return f"{_WINDOW_ACTION_PAST_TENSE[action]} '{title}'."

    def _find_window_linux(self, target: str):
        """Returns (wmctrl-style hex window id, title)."""
        if not target:
            try:
                name = subprocess.run(
                    ["xdotool", "getactivewindow", "getwindowname"],
                    shell=False, capture_output=True, text=True,
                    timeout=_SUBPROCESS_TIMEOUT_S,
                )
                wid = subprocess.run(
                    ["xdotool", "getactivewindow"],
                    shell=False, capture_output=True, text=True,
                    timeout=_SUBPROCESS_TIMEOUT_S,
                )
            except FileNotFoundError:
                raise ToolError(
                    "This needs 'xdotool', which isn't installed. Install "
                    "it with: sudo apt-get install xdotool"
                )
            except subprocess.TimeoutExpired:
                raise ToolError("Timed out finding the active window.")
            if name.returncode != 0 or wid.returncode != 0:
                raise ToolError(
                    "Couldn't determine the active window (nothing focused, "
                    "or running under Wayland without XWayland)."
                )
            return f"0x{int(wid.stdout.strip()):08x}", name.stdout.strip()

        try:
            listing = subprocess.run(
                ["wmctrl", "-l"], shell=False, capture_output=True, text=True,
                timeout=_SUBPROCESS_TIMEOUT_S,
            )
        except FileNotFoundError:
            raise ToolError(
                "This needs 'wmctrl', which isn't installed. Install it "
                "with: sudo apt-get install wmctrl"
            )
        except subprocess.TimeoutExpired:
            raise ToolError("Timed out listing windows.")
        if listing.returncode != 0:
            raise ToolError(f"wmctrl failed: {listing.stderr.strip() or 'unknown error'}")

        candidates = []
        for line in listing.stdout.splitlines():
            parts = line.split(None, 3)
            if len(parts) == 4:
                candidates.append((parts[0], parts[3]))

        matches = [(wid, t) for wid, t in candidates if target.lower() in t.lower()]
        if not matches:
            open_titles = ", ".join(repr(t) for _, t in candidates[:8]) or "none"
            raise ToolError(f"No open window matching '{target}'. Open windows: {open_titles}")
        return matches[0]

    def _control_window_linux(self, action: str, target: str) -> str:
        win_id, title = self._find_window_linux(target)
        try:
            if action == "maximize":
                result = subprocess.run(
                    ["wmctrl", "-i", "-r", win_id, "-b", "add,maximized_vert,maximized_horz"],
                    shell=False, capture_output=True, text=True, timeout=_SUBPROCESS_TIMEOUT_S,
                )
            elif action == "restore":
                subprocess.run(
                    ["wmctrl", "-i", "-r", win_id, "-b", "remove,maximized_vert,maximized_horz"],
                    shell=False, capture_output=True, text=True, timeout=_SUBPROCESS_TIMEOUT_S,
                )
                result = subprocess.run(
                    ["wmctrl", "-i", "-a", win_id],
                    shell=False, capture_output=True, text=True, timeout=_SUBPROCESS_TIMEOUT_S,
                )
            elif action == "focus":
                result = subprocess.run(
                    ["wmctrl", "-i", "-a", win_id],
                    shell=False, capture_output=True, text=True, timeout=_SUBPROCESS_TIMEOUT_S,
                )
            else:  # minimize -- wmctrl has no reliable per-window minimize
                   # across window managers; xdotool's windowminimize uses
                   # the standard ICCCM WM_CHANGE_STATE mechanism instead.
                result = subprocess.run(
                    ["xdotool", "windowminimize", str(int(win_id, 16))],
                    shell=False, capture_output=True, text=True, timeout=_SUBPROCESS_TIMEOUT_S,
                )
        except FileNotFoundError as exc:
            raise ToolError(f"Missing required tool: {exc}")
        except subprocess.TimeoutExpired:
            raise ToolError(f"Timed out trying to {action} '{title}'.")
        if result.returncode != 0:
            raise ToolError(f"Couldn't {action} '{title}': {result.stderr.strip() or 'unknown error'}")
        return f"{_WINDOW_ACTION_PAST_TENSE[action]} '{title}'."

    # ---- 6. Power management -----------------------------------------------
    #
    # Separately gated from tools.enabled (tools.power_management_enabled,
    # default False) because this is a different risk class from
    # everything above: irreversible-in-the-moment (unsaved work) rather
    # than recoverable-with-effort (a trashed file, a closed app). Every
    # shutdown/reboot still goes through ConfirmationRequired like a
    # file delete does, PLUS a minimum delay (tools.power_min_delay_seconds,
    # clamped >=5s here regardless of config) so there's always a real
    # "say cancel" window even if the confirmation itself was accepted
    # by mistake.

    def power_control(self, action: str, delay_seconds: int = 60, confirmed: bool = False) -> str:
        if not self._power_management_enabled:
            raise ToolError(
                "Power management (shutdown/reboot) isn't enabled. Set "
                "tools.power_management_enabled: true in config to turn "
                "it on -- see README's 'Power management' section for "
                "what that grants and, on Linux, the one-time setup "
                "script it needs (scripts/setup_linux_power_control.sh)."
            )
        action = (action or "").strip().lower()
        if action not in ("shutdown", "reboot", "cancel"):
            raise ToolError(f"Unknown power action '{action}'. Valid actions: shutdown, reboot, cancel.")

        if action == "cancel":
            return self._cancel_power_action()

        delay = max(self._power_min_delay_seconds, int(delay_seconds))
        if confirmed is not True and self._require_confirmation:
            verb = "Shut down" if action == "shutdown" else "Restart"
            raise ConfirmationRequired(
                f"{verb} the computer in {delay} seconds? Say cancel any "
                f"time before then to stop it.",
                action=action,
                source=f"delay={delay}s",
            )

        if sys.platform == "win32":
            return self._power_control_windows(action, delay)
        if sys.platform == "darwin":
            raise ToolError(
                "Power management isn't implemented on macOS -- not a "
                "target platform for this project (Linux Mint + Windows "
                "11 -- see README)."
            )
        return self._power_control_linux(action, delay)

    def _power_control_windows(self, action: str, delay: int) -> str:
        flag = "/s" if action == "shutdown" else "/r"
        try:
            result = subprocess.run(
                ["shutdown", flag, "/t", str(delay)],
                shell=False, capture_output=True, text=True, timeout=_SUBPROCESS_TIMEOUT_S,
            )
        except FileNotFoundError:
            raise ToolError("Couldn't find the Windows 'shutdown' command.")
        except subprocess.TimeoutExpired:
            raise ToolError("Timed out scheduling the shutdown.")
        if result.returncode != 0:
            raise ToolError(
                f"Couldn't schedule {action}: "
                f"{result.stderr.strip() or result.stdout.strip() or 'unknown error'}"
            )
        verb = "Shutting down" if action == "shutdown" else "Restarting"
        return f"{verb} in {delay} seconds. Say 'cancel' to stop it."

    def _power_control_linux(self, action: str, delay: int) -> str:
        import math

        shutdown_bin = shutil.which("shutdown") or "/sbin/shutdown"
        minutes = max(1, math.ceil(delay / 60))
        flag = "-h" if action == "shutdown" else "-r"
        try:
            # -n: fail immediately if passwordless sudo isn't set up,
            # rather than blocking on a password prompt this
            # voice-triggered, possibly-headless process can't answer --
            # see scripts/setup_linux_power_control.sh.
            result = subprocess.run(
                ["sudo", "-n", shutdown_bin, flag, f"+{minutes}"],
                shell=False, capture_output=True, text=True, timeout=_SUBPROCESS_TIMEOUT_S,
            )
        except FileNotFoundError:
            raise ToolError("Couldn't find 'sudo' or 'shutdown'.")
        except subprocess.TimeoutExpired:
            raise ToolError("Timed out scheduling the shutdown.")
        if result.returncode != 0:
            raise ToolError(
                f"Couldn't schedule {action} "
                f"({result.stderr.strip() or 'unknown error'}). This needs "
                f"passwordless sudo for exactly 'shutdown' -- run "
                f"scripts/setup_linux_power_control.sh once to set that up."
            )
        verb = "Shutting down" if action == "shutdown" else "Restarting"
        return f"{verb} in about {minutes} minute{'s' if minutes != 1 else ''}. Say 'cancel' to stop it."

    def _cancel_power_action(self) -> str:
        try:
            if sys.platform == "win32":
                result = subprocess.run(
                    ["shutdown", "/a"], shell=False, capture_output=True, text=True,
                    timeout=_SUBPROCESS_TIMEOUT_S,
                )
            else:
                shutdown_bin = shutil.which("shutdown") or "/sbin/shutdown"
                result = subprocess.run(
                    ["sudo", "-n", shutdown_bin, "-c"], shell=False, capture_output=True,
                    text=True, timeout=_SUBPROCESS_TIMEOUT_S,
                )
        except FileNotFoundError:
            raise ToolError("Couldn't find the shutdown command.")
        except subprocess.TimeoutExpired:
            raise ToolError("Timed out cancelling.")
        if result.returncode != 0:
            # Windows' `shutdown /a` returns non-zero (1116) if nothing was
            # scheduled -- from the user's side that's not a failure, just
            # "nothing to do."
            stderr = (result.stderr or result.stdout or "").strip()
            if "1116" in stderr or "no shutdown" in stderr.lower():
                return "Nothing was scheduled to cancel."
            raise ToolError(f"Couldn't cancel: {stderr or 'unknown error'}")
        return "Cancelled."

    # ---- 7. Arbitrary command execution ------------------------------------
    #
    # Separately gated from tools.enabled (tools.command_execution_enabled,
    # default False), and unlike every other method in this class, its
    # confirmation step cannot be pattern-matched away: sections 1-6 above
    # are each one specific, bounded operation (open this named app,
    # delete this one path inside allowed_roots, shut down in N seconds)
    # that can be individually reasoned about and, where safe, run without
    # asking. A shell command is opaque -- there's no reliable way to
    # classify an arbitrary string as safe vs. dangerous by inspecting it
    # (a denylist of dangerous-looking substrings is exactly the kind of
    # thing that's both bypassable and full of false positives on
    # legitimate commands). So instead of trying, every single invocation
    # is echoed back verbatim and requires the same spoken confirmation
    # file_operation's delete uses (and is controlled by the same
    # tools.require_confirmation_for_destructive flag -- turning that off
    # turns it off here too, which is worth knowing before you do it,
    # since this is the most powerful thing in this file).

    def run_command(
        self,
        command: str,
        working_directory: str = "",
        reason: str = "",
        timeout_seconds: int = 60,
        confirmed: bool = False,
    ) -> str:
        if not self._command_execution_enabled:
            raise ToolError(
                "Arbitrary command execution isn't enabled. Set "
                "tools.command_execution_enabled: true in config to turn "
                "it on. Prefer the specific tools (open_application, "
                "file_operation, control_window, ...) when one of them "
                "already covers the request -- this is the fallback for "
                "everything else."
            )
        command = (command or "").strip()
        if not command:
            raise ToolError("run_command requires a non-empty 'command'.")

        cwd: Optional[Path] = None
        if working_directory:
            cwd = Path(working_directory).expanduser().resolve()
            _check_within_allowed_roots(cwd, self._allowed_roots)
            if not cwd.is_dir():
                raise ToolError(f"'{cwd}' isn't a directory.")

        if confirmed is not True and self._require_confirmation:
            where = f" in {cwd}" if cwd else ""
            why = f" ({reason})" if reason else ""
            raise ConfirmationRequired(
                f"Run this command{where}{why}: {command}",
                action="run_command",
                source=command,
            )

        if sys.platform == "win32":
            argv = ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command]
        else:
            argv = ["bash", "-c", command]

        try:
            result = subprocess.run(
                argv,
                shell=False,
                cwd=str(cwd) if cwd else None,
                capture_output=True,
                text=True,
                timeout=max(5, timeout_seconds),
            )
        except FileNotFoundError:
            shell_name = "powershell.exe" if sys.platform == "win32" else "bash"
            raise ToolError(f"Couldn't find {shell_name} to run the command.")
        except subprocess.TimeoutExpired:
            raise ToolError(f"Command timed out after {timeout_seconds}s: {command}")

        output = ((result.stdout or "") + (result.stderr or "")).strip()
        if len(output) > 800:
            output = output[:800] + f"... [{len(output) - 800} more characters truncated]"

        status = "succeeded" if result.returncode == 0 else f"exited with code {result.returncode}"
        if output:
            return f"Command {status}. Output: {output}"
        return f"Command {status} with no output."

    # ---- 8. Durable user profile (name, preferences) -----------------------
    #
    # Distinct from conversation memory, which is a recent-dialogue
    # rolling window that gets compacted/summarized as it grows -- see
    # UserProfileStore's docstring. Always available whenever tools are
    # enabled (no separate opt-in flag, unlike sections 6/7): storing a
    # fact in a local JSON file the user could open and read themselves
    # isn't in the same risk class as running arbitrary commands or
    # powering off the machine.

    def remember_user_fact(self, key: str, value: str) -> str:
        if self._user_profile is None:
            raise ToolError("User profile storage isn't configured.")
        try:
            self._user_profile.remember(key, value)
        except ValueError as exc:
            raise ToolError(str(exc))
        return f"Remembered: {key.strip().lower().replace(' ', '_').replace('_', ' ')} = {value.strip()}."

    def forget_user_fact(self, key: str) -> str:
        if self._user_profile is None:
            raise ToolError("User profile storage isn't configured.")
        removed = self._user_profile.forget(key)
        label = key.strip().lower().replace(" ", "_").replace("_", " ")
        return f"Forgot {label}." if removed else f"I didn't have anything remembered for '{key}'."

    # ---- 9. YouTube search-and-play -----------------------------------------
    #
    # "Play the first video on YouTube after searching for X" needs a
    # real video URL, not a search-results page -- search_web(site=
    # "youtube") only ever gets you the latter, which is why this is a
    # separate tool rather than another search_web site option.
    #
    # Resolved via yt-dlp, not by parsing YouTube's search-results HTML
    # directly: yt-dlp is a real, actively-maintained resolver built and
    # kept up to date for exactly this (YouTube changes its page
    # structure often enough that a hand-rolled scraper breaks
    # regularly; yt-dlp's maintainers absorb that churn continuously).
    # Worth knowing plainly: this is not the official YouTube Data API
    # (no API key, no daily quota, but also not an officially sanctioned
    # integration path the way the Data API is) -- if that tradeoff
    # matters for your use case, the Data API is the alternative, at the
    # cost of needing a Google Cloud project and living within its quota.

    def play_youtube_search(self, query: str) -> str:
        query = (query or "").strip()
        if not query:
            raise ToolError("play_youtube_search requires a non-empty query.")

        try:
            import yt_dlp
        except ImportError:
            raise ToolError(
                "yt-dlp isn't installed, so I can't resolve a direct "
                "video URL. Install it with: pip install yt-dlp"
            )

        ydl_opts = {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "extract_flat": "in_playlist",
            "socket_timeout": 8,
        }
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(f"ytsearch1:{query}", download=False)
        except yt_dlp.utils.DownloadError as exc:
            raise ToolError(f"Couldn't search YouTube for '{query}': {exc}")
        except Exception as exc:  # noqa: BLE001 -- yt-dlp's exception surface is broad
            raise ToolError(f"Something went wrong searching YouTube for '{query}': {exc}")

        entries = (info or {}).get("entries") or []
        if not entries:
            raise ToolError(f"No YouTube results for '{query}'.")

        first = entries[0]
        video_id = first.get("id")
        if not video_id:
            raise ToolError(f"Found a result for '{query}' but couldn't get its video ID.")

        webbrowser.open(_validated_web_url(f"https://www.youtube.com/watch?v={urllib.parse.quote(str(video_id))}"))
        title = first.get("title") or query
        return f"Playing '{title}' on YouTube."
