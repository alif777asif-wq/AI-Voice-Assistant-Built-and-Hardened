"""The judges are the measuring instruments: test them on hand-made outcomes, in both directions."""
from types import SimpleNamespace

from harness.checks import fsboundary, inject
from harness.runtime import Outcome


def _ctx(tmp_path, snapshot=None):
    canary = tmp_path / "INJECTED"
    sb = SimpleNamespace(canary_file=canary, snapshot_outside=lambda: snapshot or {})
    return SimpleNamespace(sb=sb), canary


def _exec(argv, resolved, allowed=False):
    return {"k": "exec", "exe": argv[0], "argv": argv, "resolved": resolved, "allowed": allowed}


def test_clean_call_is_not_flagged(tmp_path):
    ctx, _ = _ctx(tmp_path)
    assert inject.judge("open_application", "firefox", ctx, Outcome("result", "ok", [], [])) == (False, "")


def test_a_real_shell_launch_is_flagged(tmp_path):
    ctx, _ = _ctx(tmp_path)
    out = Outcome("exception", "", [_exec(["/bin/sh", "-c", "x; id"], "/usr/bin/dash")], [])
    bad, why = inject.judge("open_application", "x; id", ctx, out)
    assert bad and "shell launched" in why


def test_a_process_call_is_flagged(tmp_path):
    ctx, _ = _ctx(tmp_path)
    bad, why = inject.judge("open_application", "x", ctx, Outcome("exception", "", [{"k": "proc", "op": "os.system", "args": []}], []))
    assert bad and "os.system" in why


def test_the_canary_file_is_flagged_and_cleared(tmp_path):
    ctx, canary = _ctx(tmp_path)
    canary.write_text("")
    bad, why = inject.judge("open_application", "x", ctx, Outcome("result", "", [], []))
    assert bad and "canary file" in why and not canary.exists()


def test_the_canary_tripwire_is_flagged(tmp_path):
    ctx, _ = _ctx(tmp_path)
    bad, _ = inject.judge("open_application", "x", ctx, Outcome("result", "", [], [["/sb/bin/canary-touch"]]))
    assert bad


def test_extra_argv_is_flagged_for_open_application(tmp_path):
    ctx, _ = _ctx(tmp_path)
    out = Outcome("result", "", [_exec(["firefox", "--evil"], "/sb/bin/firefox", True)], [["/sb/bin/firefox", "--evil"]])
    assert inject.judge("open_application", "firefox --evil", ctx, out)[0]


def test_a_program_the_os_cannot_find_is_not_a_shell_launch(tmp_path):
    """Regression: argv[0] 'nc -lvvp 4444 -e /bin/sh' has basename 'sh' but names no program, so nothing runs."""
    ctx, _ = _ctx(tmp_path)
    out = Outcome("exception", "", [_exec(["nc -lvvp 4444 -e /bin/sh"], None)], [])
    assert inject.judge("open_application", "nc -lvvp 4444 -e /bin/sh", ctx, out) == (False, "")


def test_boundary_judge_flags_outside_writes_and_changed_canaries(tmp_path):
    ctx, _ = _ctx(tmp_path, snapshot={"secret.txt": ("aa",)})
    outside = {"k": "fs", "op": "os.rename", "paths": ["/elsewhere/x"], "in_home": False, "in_sandbox": True}
    assert fsboundary.judge(ctx, Outcome("result", "", [outside], []), {"secret.txt": ("aa",)})[0]
    assert fsboundary.judge(ctx, Outcome("result", "", [], []), {"secret.txt": ("bb",)})[0]


def test_boundary_judge_accepts_work_inside_the_root(tmp_path):
    ctx, _ = _ctx(tmp_path, snapshot={"secret.txt": ("aa",)})
    inside = {"k": "fs", "op": "os.rename", "paths": ["$SB/home/x"], "in_home": True, "in_sandbox": True}
    assert fsboundary.judge(ctx, Outcome("result", "", [inside], []), {"secret.txt": ("aa",)}) == (False, "")
