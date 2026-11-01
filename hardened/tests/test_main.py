"""
main.py is otherwise thin wiring (see its own docstring) and not
directly unit tested elsewhere in this suite -- but the autostart CLI
handling in cli_main() is real branching logic, not just wiring, so it
gets its own small, focused test file rather than being trusted
untested.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from l_assistant.main import cli_main


def test_enable_autostart_on_non_windows_exits_cleanly(monkeypatch, capsys):
    monkeypatch.setattr(sys, "platform", "linux")
    with pytest.raises(SystemExit) as exc_info:
        cli_main(["--enable-autostart"])
    assert exc_info.value.code == 1
    assert "Windows-only" in capsys.readouterr().err


def test_disable_autostart_on_non_windows_exits_cleanly(monkeypatch, capsys):
    monkeypatch.setattr(sys, "platform", "linux")
    with pytest.raises(SystemExit) as exc_info:
        cli_main(["--disable-autostart"])
    assert exc_info.value.code == 1
    assert "Windows-only" in capsys.readouterr().err


class _FakeWinreg:
    HKEY_CURRENT_USER = object()
    KEY_READ = 1
    KEY_WRITE = 2
    REG_SZ = 1

    def __init__(self):
        self.values: dict = {}
        self.key_existed = False

    def CreateKeyEx(self, hive, path, reserved, access):
        self.key_existed = True
        return self

    def OpenKey(self, hive, path, reserved, access):
        if not self.key_existed:
            raise FileNotFoundError(path)
        return self

    def SetValueEx(self, key_handle, name, reserved, value_type, data):
        self.values[name] = data

    def QueryValueEx(self, key_handle, name):
        if name not in self.values:
            raise FileNotFoundError(name)
        return (self.values[name], self.REG_SZ)

    def DeleteValue(self, key_handle, name):
        if name not in self.values:
            raise FileNotFoundError(name)
        del self.values[name]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture
def fake_windows_registry(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    fake = _FakeWinreg()
    monkeypatch.setitem(sys.modules, "winreg", fake)
    return fake


def test_enable_autostart_on_windows_writes_registry_and_returns_normally(
    fake_windows_registry, capsys
):
    cli_main(["--enable-autostart"])  # must NOT raise/exit -- normal return = success
    assert "L Voice Assistant" in fake_windows_registry.values
    assert "start automatically" in capsys.readouterr().out


def test_disable_autostart_on_windows_removes_registry_value(fake_windows_registry, capsys):
    fake_windows_registry.key_existed = True
    fake_windows_registry.values["L Voice Assistant"] = "some command"
    cli_main(["--disable-autostart"])
    assert "L Voice Assistant" not in fake_windows_registry.values
    assert "no longer start automatically" in capsys.readouterr().out


def test_enable_and_disable_autostart_are_mutually_exclusive():
    with pytest.raises(SystemExit):
        cli_main(["--enable-autostart", "--disable-autostart"])


# ================================================ unhandled startup errors


def test_show_fatal_error_falls_back_to_stderr_on_non_windows(monkeypatch, capsys):
    from l_assistant.main import _show_fatal_error

    monkeypatch.setattr(sys, "platform", "linux")
    _show_fatal_error("Test Title", "Test message")
    captured = capsys.readouterr()
    assert "Test Title" in captured.err
    assert "Test message" in captured.err


def test_show_fatal_error_calls_message_box_on_windows(monkeypatch):
    from l_assistant.main import _show_fatal_error

    monkeypatch.setattr(sys, "platform", "win32")
    calls = []

    class FakeUser32:
        def MessageBoxW(self, hwnd, text, title, flags):
            calls.append((text, title, flags))

    class FakeWindll:
        user32 = FakeUser32()

    monkeypatch.setattr(sys.modules["ctypes"], "windll", FakeWindll(), raising=False)
    _show_fatal_error("Test Title", "Test message")
    assert len(calls) == 1
    text, title, flags = calls[0]
    assert title == "Test Title"
    assert "Test message" in text


def test_show_fatal_error_never_raises_even_if_message_box_fails(monkeypatch):
    from l_assistant.main import _show_fatal_error

    monkeypatch.setattr(sys, "platform", "win32")

    class BrokenUser32:
        def MessageBoxW(self, *a):
            raise OSError("no display")

    class BrokenWindll:
        user32 = BrokenUser32()

    monkeypatch.setattr(sys.modules["ctypes"], "windll", BrokenWindll(), raising=False)
    _show_fatal_error("Title", "Message")  # must not raise -- this IS the last resort


def test_unexpected_exception_during_startup_is_caught_not_silent(monkeypatch, tmp_path, capsys):
    """
    Reproduces the actual bug: something raises a type NOT covered by
    the specific (FileNotFoundError, ValueError, RuntimeError) tuple
    during tray/foreground startup. Before the fix, this propagated all
    the way past cli_main() uncaught -- fine with a console attached
    (a visible traceback), completely silent with console=False (see
    l_agent.spec), which is exactly the packaged build's configuration.
    This test proves cli_main() now catches it, logs it, and exits
    cleanly instead of letting it escape.
    """
    import l_assistant.main as main_module

    def _explode(config):
        raise KeyError("something pystray-shaped and unexpected")

    monkeypatch.setattr(main_module, "_run_tray_mode", _explode)
    monkeypatch.setattr(sys, "platform", "linux")  # exercises the stderr fallback path

    config_path = tmp_path / "config.yaml"
    config_path.write_text("tray:\n  enabled: true\n", encoding="utf-8")

    with pytest.raises(SystemExit) as exc_info:
        cli_main(["--config", str(config_path)])

    assert exc_info.value.code == 1
    # The fatal-error surfacing actually ran (stderr fallback on this
    # non-Windows path) -- this is the assertion that would have failed
    # before the fix, when the KeyError instead propagated straight out
    # of cli_main() as an uncaught exception rather than being caught
    # and reported here.
    assert "failed to start" in capsys.readouterr().err.lower()
