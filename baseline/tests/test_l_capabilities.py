import subprocess as real_subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from l_assistant.tools import system_tools
from l_assistant.tools.system_tools import ConfirmationRequired, SystemToolsEngine, ToolError


class _FakeSubprocessModule:
    """Swaps out system_tools' `subprocess` name for a stub `.run`, while
    keeping the real exception/constant classes so the module's own
    `except subprocess.TimeoutExpired` etc. still matches correctly."""

    def __init__(self, run_fn):
        self.run = run_fn
        self.DEVNULL = real_subprocess.DEVNULL
        self.TimeoutExpired = real_subprocess.TimeoutExpired
        self.CompletedProcess = real_subprocess.CompletedProcess
        self.Popen = real_subprocess.Popen


def _completed(returncode=0, stdout="", stderr=""):
    return real_subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


@pytest.fixture
def sandbox(tmp_path: Path) -> Path:
    d = tmp_path / "sandbox"
    d.mkdir()
    return d


# =========================================================================
# run_command
# =========================================================================


def test_run_command_disabled_by_default(sandbox: Path):
    engine = SystemToolsEngine(allowed_roots=[str(sandbox)])
    with pytest.raises(ToolError, match="isn't enabled"):
        engine.run_command(command="echo hi")


def test_run_command_requires_confirmation_and_echoes_literal_command(sandbox: Path):
    engine = SystemToolsEngine(allowed_roots=[str(sandbox)], command_execution_enabled=True)
    with pytest.raises(ConfirmationRequired) as exc_info:
        engine.run_command(command="rm -rf /tmp/whatever", reason="cleanup")
    message = str(exc_info.value)
    assert "rm -rf /tmp/whatever" in message
    assert "cleanup" in message


def test_run_command_respects_require_confirmation_false(sandbox: Path):
    # Same dial file_operation's delete already uses -- deliberately
    # reused rather than made un-loosenable, see the section 7 docstring
    # in system_tools.py.
    engine = SystemToolsEngine(
        allowed_roots=[str(sandbox)],
        command_execution_enabled=True,
        require_confirmation=False,
    )
    result = engine.run_command(command="echo no confirmation needed")
    assert "succeeded" in result
    assert "no confirmation needed" in result


def test_run_command_actually_executes_real_bash_on_linux(sandbox: Path):
    if sys.platform == "win32":
        pytest.skip("this exercises the Linux bash -c path")
    engine = SystemToolsEngine(allowed_roots=[str(sandbox)], command_execution_enabled=True)
    result = engine.run_command(command="echo hello-from-run-command", confirmed=True)
    assert "succeeded" in result
    assert "hello-from-run-command" in result


def test_run_command_reports_nonzero_exit(sandbox: Path):
    if sys.platform == "win32":
        pytest.skip("this exercises the Linux bash -c path")
    engine = SystemToolsEngine(allowed_roots=[str(sandbox)], command_execution_enabled=True)
    result = engine.run_command(command="exit 7", confirmed=True)
    assert "exited with code 7" in result


def test_run_command_truncates_long_output(sandbox: Path):
    if sys.platform == "win32":
        pytest.skip("this exercises the Linux bash -c path")
    engine = SystemToolsEngine(allowed_roots=[str(sandbox)], command_execution_enabled=True)
    result = engine.run_command(
        command="python3 -c \"print('x' * 5000)\"", confirmed=True
    )
    assert "more characters truncated" in result
    assert len(result) < 2000


def test_run_command_working_directory_outside_allowed_roots_refused(sandbox: Path, tmp_path: Path):
    engine = SystemToolsEngine(allowed_roots=[str(sandbox)], command_execution_enabled=True)
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    with pytest.raises(ToolError, match="outside the allowed"):
        engine.run_command(command="echo hi", working_directory=str(outside), confirmed=True)


def test_run_command_timeout(monkeypatch, sandbox: Path):
    def fake_run(*a, **k):
        raise real_subprocess.TimeoutExpired(cmd="sleep 100", timeout=1)

    monkeypatch.setattr(system_tools, "subprocess", _FakeSubprocessModule(fake_run))
    engine = SystemToolsEngine(allowed_roots=[str(sandbox)], command_execution_enabled=True)
    with pytest.raises(ToolError, match="timed out"):
        engine.run_command(command="sleep 100", confirmed=True)


# =========================================================================
# power_control
# =========================================================================


def test_power_control_disabled_by_default(sandbox: Path):
    engine = SystemToolsEngine(allowed_roots=[str(sandbox)])
    with pytest.raises(ToolError, match="isn't enabled"):
        engine.power_control(action="shutdown")


def test_power_control_requires_confirmation(sandbox: Path):
    engine = SystemToolsEngine(allowed_roots=[str(sandbox)], power_management_enabled=True)
    with pytest.raises(ConfirmationRequired, match="Shut down"):
        engine.power_control(action="shutdown", delay_seconds=60)


def test_power_control_enforces_minimum_delay(sandbox: Path):
    engine = SystemToolsEngine(
        allowed_roots=[str(sandbox)],
        power_management_enabled=True,
        power_min_delay_seconds=30,
    )
    with pytest.raises(ConfirmationRequired, match="30 seconds"):
        # asked for 1 second, must be clamped up to the configured minimum
        engine.power_control(action="shutdown", delay_seconds=1)


def test_power_control_linux_uses_noninteractive_sudo(monkeypatch, sandbox: Path):
    if sys.platform == "win32":
        pytest.skip("this exercises the Linux sudo -n path")
    captured = {}

    def fake_run(argv, **kwargs):
        captured["argv"] = argv
        return _completed(returncode=0)

    monkeypatch.setattr(system_tools, "subprocess", _FakeSubprocessModule(fake_run))
    engine = SystemToolsEngine(allowed_roots=[str(sandbox)], power_management_enabled=True)
    result = engine.power_control(action="shutdown", delay_seconds=120, confirmed=True)

    assert captured["argv"][:3] == ["sudo", "-n", captured["argv"][2]]
    assert "-n" in captured["argv"]
    assert "shutdown" in captured["argv"][2]
    assert "-h" in captured["argv"]
    assert "+2" in captured["argv"]  # 120s -> 2 minutes
    assert "about 2 minutes" in result


def test_power_control_linux_missing_sudo_rule_gives_actionable_error(monkeypatch, sandbox: Path):
    if sys.platform == "win32":
        pytest.skip("this exercises the Linux sudo -n path")

    def fake_run(argv, **kwargs):
        return _completed(returncode=1, stderr="sudo: a password is required")

    monkeypatch.setattr(system_tools, "subprocess", _FakeSubprocessModule(fake_run))
    engine = SystemToolsEngine(allowed_roots=[str(sandbox)], power_management_enabled=True)
    with pytest.raises(ToolError, match="setup_linux_power_control.sh"):
        engine.power_control(action="reboot", delay_seconds=30, confirmed=True)


def test_power_control_cancel_needs_no_confirmation(monkeypatch, sandbox: Path):
    def fake_run(argv, **kwargs):
        return _completed(returncode=0)

    monkeypatch.setattr(system_tools, "subprocess", _FakeSubprocessModule(fake_run))
    engine = SystemToolsEngine(allowed_roots=[str(sandbox)], power_management_enabled=True)
    # No ConfirmationRequired raised, no confirmed=True needed:
    result = engine.power_control(action="cancel")
    assert result == "Cancelled."


def test_power_control_cancel_nothing_scheduled_not_an_error(monkeypatch, sandbox: Path):
    def fake_run(argv, **kwargs):
        return _completed(returncode=1, stderr="shutdown: 1116: No shutdown in progress")

    monkeypatch.setattr(system_tools, "subprocess", _FakeSubprocessModule(fake_run))
    engine = SystemToolsEngine(allowed_roots=[str(sandbox)], power_management_enabled=True)
    result = engine.power_control(action="cancel")
    assert "Nothing was scheduled" in result


# =========================================================================
# control_window (Linux branch -- this sandbox has no ctypes.windll, so
# the Windows branch can only be reviewed, not executed, here)
# =========================================================================

_WMCTRL_LIST = (
    "0x02c00003  0 host  Brave Browser - Reddit\n"
    "0x03400002  0 host  Visual Studio Code\n"
)


def test_control_window_unknown_action_rejected(sandbox: Path):
    engine = SystemToolsEngine(allowed_roots=[str(sandbox)])
    with pytest.raises(ToolError, match="Unknown window action"):
        engine.control_window(action="teleport", target="Brave")


def test_control_window_maximize_by_title_match(monkeypatch, sandbox: Path):
    if sys.platform == "win32":
        pytest.skip("this exercises the Linux wmctrl path")
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        if argv[:2] == ["wmctrl", "-l"]:
            return _completed(returncode=0, stdout=_WMCTRL_LIST)
        return _completed(returncode=0)

    monkeypatch.setattr(system_tools, "subprocess", _FakeSubprocessModule(fake_run))
    engine = SystemToolsEngine(allowed_roots=[str(sandbox)])
    result = engine.control_window(action="maximize", target="brave")

    assert "Maximized" in result
    assert "Brave Browser - Reddit" in result
    maximize_call = calls[-1]
    assert maximize_call[:3] == ["wmctrl", "-i", "-r"]
    assert "0x02c00003" in maximize_call
    assert "add,maximized_vert,maximized_horz" in maximize_call


def test_control_window_no_match_lists_open_windows(monkeypatch, sandbox: Path):
    if sys.platform == "win32":
        pytest.skip("this exercises the Linux wmctrl path")

    def fake_run(argv, **kwargs):
        return _completed(returncode=0, stdout=_WMCTRL_LIST)

    monkeypatch.setattr(system_tools, "subprocess", _FakeSubprocessModule(fake_run))
    engine = SystemToolsEngine(allowed_roots=[str(sandbox)])
    with pytest.raises(ToolError) as exc_info:
        engine.control_window(action="focus", target="spotify")
    message = str(exc_info.value)
    assert "No open window matching 'spotify'" in message
    assert "Visual Studio Code" in message  # helpful listing of what IS open


def test_control_window_minimize_uses_xdotool_with_decimal_id(monkeypatch, sandbox: Path):
    if sys.platform == "win32":
        pytest.skip("this exercises the Linux xdotool path")
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        if argv[:2] == ["wmctrl", "-l"]:
            return _completed(returncode=0, stdout=_WMCTRL_LIST)
        return _completed(returncode=0)

    monkeypatch.setattr(system_tools, "subprocess", _FakeSubprocessModule(fake_run))
    engine = SystemToolsEngine(allowed_roots=[str(sandbox)])
    result = engine.control_window(action="minimize", target="Visual Studio Code")

    assert "Minimized" in result
    minimize_call = calls[-1]
    assert minimize_call[0] == "xdotool"
    assert minimize_call[1] == "windowminimize"
    assert minimize_call[2] == str(int("0x03400002", 16))  # decimal, not hex


def test_control_window_missing_wmctrl_gives_install_hint(monkeypatch, sandbox: Path):
    if sys.platform == "win32":
        pytest.skip("this exercises the Linux wmctrl path")

    def fake_run(argv, **kwargs):
        raise FileNotFoundError("wmctrl")

    monkeypatch.setattr(system_tools, "subprocess", _FakeSubprocessModule(fake_run))
    engine = SystemToolsEngine(allowed_roots=[str(sandbox)])
    with pytest.raises(ToolError, match="apt-get install wmctrl"):
        engine.control_window(action="focus", target="brave")
