"""The jail must record the attempt AND refuse to let it touch the real machine."""
import subprocess
import sys

import pytest

from harness.jail import Jail
from harness.sandbox import Sandbox


@pytest.fixture(scope="module")
def rig():
    sb = Sandbox(app_tokens=["firefox"])
    jail = Jail(sb)
    sb.jail = jail
    jail.install()                 # an audit hook cannot be removed; it only acts while armed
    yield sb, jail
    jail.armed = False
    sb.cleanup()


def _armed(jail):
    class Ctx:
        def __enter__(self):
            jail.armed = True

        def __exit__(self, *exc):
            jail.armed = False
    return Ctx()


def test_a_real_program_is_vetoed_but_recorded(rig):
    sb, jail = rig
    mark = jail.mark()
    with _armed(jail), pytest.raises(PermissionError):
        subprocess.Popen(["/bin/sh", "-c", "true"])
    ev = [e for e in jail.since(mark) if e["k"] == "exec"]
    assert ev and ev[0]["allowed"] is False and ev[0]["resolved"]


def test_a_sandbox_tripwire_runs_and_is_logged_by_the_tripwire_itself(rig):
    sb, jail = rig
    mark, tmark = jail.mark(), sb.log_mark()
    with _armed(jail):
        proc = subprocess.Popen([str(sb.bin / "firefox"), "--probe"],
                                env={"TRIPWIRE_LOG": str(sb.tripwire_log), "PATH": str(sb.bin)})
    assert proc.wait(10) == 0
    ev = [e for e in jail.since(mark) if e["k"] == "exec"]
    assert ev and ev[0]["allowed"] and jail.is_tripwire(ev[0]["resolved"])
    records = sb.log_since(tmark)
    assert len(records) == 1 and records[0][1:] == ["--probe"]


def test_os_system_is_vetoed(rig):
    sb, jail = rig
    mark = jail.mark()
    import os
    with _armed(jail), pytest.raises(PermissionError):
        os.system("true")
    assert any(e["k"] == "proc" for e in jail.since(mark))


def test_writes_outside_the_sandbox_are_vetoed_and_inside_are_allowed(rig, tmp_path_factory):
    sb, jail = rig
    outside = tmp_path_factory.mktemp("real-machine") / "x.txt"
    with _armed(jail):
        with pytest.raises(PermissionError):
            open(outside, "w")
        (sb.home / "ok.txt").write_text("fine")
    assert not outside.exists() and (sb.home / "ok.txt").read_text() == "fine"


def test_network_connections_are_vetoed(rig):
    import socket
    sb, jail = rig
    mark = jail.mark()
    with _armed(jail), pytest.raises(PermissionError):
        socket.create_connection(("127.0.0.1", 9), timeout=1)
    assert any(e["k"] == "net" for e in jail.since(mark))


def test_nothing_is_recorded_when_disarmed(rig):
    sb, jail = rig
    mark = jail.mark()
    subprocess.run([sys.executable, "-c", "pass"], check=True)
    assert jail.since(mark) == []
