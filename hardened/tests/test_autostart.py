"""
autostart.py's public functions are tested two ways:
- On any platform: build_command_line() (pure logic) and the
  RuntimeError guard on non-Windows.
- Via a fake winreg (module-level, in-memory dict standing in for the
  registry) injected into sys.modules: the actual enable/disable/toggle
  logic, exercised as it would really run on Windows, without needing a
  real Windows registry. sys.platform is patched to "win32" for the
  duration so is_available()'s guard doesn't short-circuit these tests
  on this (Linux) sandbox.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import l_assistant.autostart as autostart


# ============================================================= pure logic


def test_build_command_line_from_source():
    cmd = autostart.build_command_line()
    assert cmd == f'"{sys.executable}" -m l_assistant --tray'


def test_build_command_line_explicit_exe_path():
    cmd = autostart.build_command_line(Path(r"C:\Program Files\L\L.exe"))
    assert cmd == '"C:\\Program Files\\L\\L.exe" --tray'


def test_build_command_line_frozen_build(monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", r"C:\Program Files\L\L.exe")
    cmd = autostart.build_command_line()
    assert cmd == '"C:\\Program Files\\L\\L.exe" --tray'


def test_functions_refuse_to_run_on_non_windows(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    for fn in (autostart.is_enabled, autostart.disable, autostart.enable, autostart.toggle):
        with pytest.raises(RuntimeError, match="Windows-only"):
            fn()


# ================================================================ fake winreg


class _FakeWinreg:
    """In-memory stand-in for the subset of winreg's API autostart.py
    actually uses. HKEY_CURRENT_USER is just a sentinel object here --
    real values/types don't matter, only that the same object is passed
    through consistently, matching how winreg's real handle objects work."""

    HKEY_CURRENT_USER = object()
    KEY_READ = 1
    KEY_WRITE = 2
    REG_SZ = 1

    def __init__(self):
        self.values: dict = {}  # {value_name: data}
        self.key_existed = False

    def CreateKeyEx(self, hive, path, reserved, access):
        self.key_existed = True
        return _FakeKeyHandle(self)

    def OpenKey(self, hive, path, reserved, access):
        if not self.key_existed:
            raise FileNotFoundError(f"no such key: {path}")
        return _FakeKeyHandle(self)

    def SetValueEx(self, key_handle, name, reserved, value_type, data):
        key_handle.store.values[name] = data

    def QueryValueEx(self, key_handle, name):
        if name not in key_handle.store.values:
            raise FileNotFoundError(name)
        return (key_handle.store.values[name], _FakeWinreg.REG_SZ)

    def DeleteValue(self, key_handle, name):
        if name not in key_handle.store.values:
            raise FileNotFoundError(name)
        del key_handle.store.values[name]


class _FakeKeyHandle:
    def __init__(self, store: _FakeWinreg):
        self.store = store

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture
def fake_registry(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    fake = _FakeWinreg()
    monkeypatch.setitem(sys.modules, "winreg", fake)
    return fake


def test_starts_disabled(fake_registry):
    assert autostart.is_enabled() is False


def test_enable_then_is_enabled(fake_registry):
    autostart.enable()
    assert autostart.is_enabled() is True
    assert fake_registry.values[autostart._VALUE_NAME] == autostart.build_command_line()


def test_enable_with_explicit_exe_path(fake_registry):
    exe = Path(r"C:\Program Files\L\L.exe")
    autostart.enable(exe)
    assert fake_registry.values[autostart._VALUE_NAME] == autostart.build_command_line(exe)


def test_disable_after_enable(fake_registry):
    autostart.enable()
    autostart.disable()
    assert autostart.is_enabled() is False


def test_disable_when_never_enabled_is_not_an_error(fake_registry):
    autostart.disable()  # must not raise
    assert autostart.is_enabled() is False


def test_toggle_flips_state_and_returns_new_state(fake_registry):
    assert autostart.toggle() is True
    assert autostart.is_enabled() is True
    assert autostart.toggle() is False
    assert autostart.is_enabled() is False
