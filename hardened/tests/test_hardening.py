"""Regression tests for the tool-layer hardening (one block per weakness found by the adversarial harness)."""
import os
import subprocess as real_subprocess
import sys
from pathlib import Path

import pytest

# L_ASSISTANT_SRC lets the repository's audit run this same file against the untouched baseline,
# to show these tests really fail on the original code.
sys.path.insert(0, os.environ.get("L_ASSISTANT_SRC") or str(Path(__file__).resolve().parent.parent / "src"))

from l_assistant.tools import system_tools
from l_assistant.tools.registry import ToolDispatcher
from l_assistant.tools.system_tools import ConfirmationRequired, SystemToolsEngine, ToolError


@pytest.fixture
def home(tmp_path: Path) -> Path:
    d = tmp_path / "home"
    d.mkdir()
    (d / "a.txt").write_text("A")
    (d / "b.txt").write_text("B")
    return d


@pytest.fixture
def trash(tmp_path, monkeypatch):
    """Capture send2trash instead of touching a real trash can."""
    moved = []

    def fake_send2trash(path):
        moved.append(Path(path))
        p = Path(path)
        p.unlink() if p.is_file() else __import__("shutil").rmtree(p)

    import send2trash

    monkeypatch.setattr(send2trash, "send2trash", fake_send2trash)
    return moved


# ---- 1. the model cannot confirm for itself --------------------------------------------------


@pytest.mark.parametrize("value", [True, "true", "false", 1, "yes"])
def test_model_supplied_confirmed_is_ignored(home, value, trash):
    engine = SystemToolsEngine(allowed_roots=[str(home)])
    dispatcher = ToolDispatcher(engine)
    with pytest.raises(ConfirmationRequired):
        dispatcher.execute("file_operation", {"action": "delete", "source": str(home / "a.txt"), "confirmed": value})
    assert (home / "a.txt").exists() and not trash


def test_trusted_keyword_confirmation_still_works(home, trash):
    dispatcher = ToolDispatcher(SystemToolsEngine(allowed_roots=[str(home)]))
    result = dispatcher.execute("file_operation", {"action": "delete", "source": str(home / "a.txt")}, confirmed=True)
    assert "trash" in result.lower() and not (home / "a.txt").exists()


def test_trusted_confirmation_is_dropped_for_tools_without_the_parameter(home):
    dispatcher = ToolDispatcher(SystemToolsEngine(allowed_roots=[str(home)]))
    assert dispatcher.execute("minimize_all_windows", {}, confirmed=True).startswith(("Error", "Minimized"))


def test_engine_requires_exactly_true_not_a_truthy_value(home, trash):
    engine = SystemToolsEngine(allowed_roots=[str(home)])
    for truthy in ("false", 1, "yes", [1]):
        with pytest.raises(ConfirmationRequired):
            engine.file_operation(action="delete", source=str(home / "a.txt"), confirmed=truthy)
    assert (home / "a.txt").exists()


def test_non_dict_arguments_are_an_error_string_not_a_crash(home):
    dispatcher = ToolDispatcher(SystemToolsEngine(allowed_roots=[str(home)]))
    assert dispatcher.execute("open_website", ["x"]).startswith("Error")


# ---- 2. close_application signals exactly the process named -------------------------------------


def test_close_application_passes_an_escaped_pattern_after_double_dash(monkeypatch, home):
    captured = {}

    def fake_run(argv, **kwargs):
        captured["argv"] = argv
        return real_subprocess.CompletedProcess(argv, 0, "", "")

    class _Sub:
        run = staticmethod(fake_run)
        TimeoutExpired = real_subprocess.TimeoutExpired
        DEVNULL = real_subprocess.DEVNULL

    monkeypatch.setattr(system_tools, "subprocess", _Sub)
    monkeypatch.setattr(system_tools.sys, "platform", "linux")
    SystemToolsEngine(allowed_roots=[str(home)]).close_application("nextpad++")
    assert captured["argv"] == ["pkill", "-x", "--", r"nextpad\+\+"]


# ---- 3. open_application takes a bare name, never a path or an option -------------------------------


# ---- 4. only http(s) reaches the browser ----------------------------------------------------------------------


def test_open_website_never_calls_the_browser_for_a_refused_target(monkeypatch, home):
    opened = []
    monkeypatch.setattr(system_tools.webbrowser, "open", lambda url: opened.append(url))
    engine = SystemToolsEngine(allowed_roots=[str(home)])
    with pytest.raises(ToolError):
        engine.open_website("file:///home/u/.ssh/id_rsa")
    with pytest.raises(ToolError):
        engine.open_website("--renderer-cmd-prefix=calc://x.y")
    assert opened == []
    engine.open_website("example.com")
    engine.open_website("github")
    assert opened == ["https://example.com", "https://github.com"]


# ---- 5. protected locations ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "rel",
    [".ssh/authorized_keys", ".ssh/config", ".config/autostart/x.desktop", ".config/systemd/user/x.service", ".profile",
     ".zshrc", ".bash_profile", ".aws/credentials", ".netrc", ".git/hooks/pre-commit", ".npmrc",
     "AppData/Roaming/Microsoft/Windows/Start Menu/Programs/Startup/run.bat", "Library/LaunchAgents/x.plist"],
)
@pytest.mark.parametrize("action", ["create", "copy", "rename"])
def test_protected_locations_are_refused(home, rel, action):
    (home / "src.txt").write_text("payload")
    engine = SystemToolsEngine(allowed_roots=[str(home)])
    kwargs = {
        "create": {"action": "create", "source": str(home / rel), "content": "x"},
        "copy": {"action": "copy", "source": str(home / "src.txt"), "destination": str(home / rel)},
        "rename": {"action": "rename", "source": str(home / "src.txt"), "destination": str(home / rel), "confirmed": True},
    }[action]
    with pytest.raises(ToolError, match="protected location"):
        engine.file_operation(**kwargs)
    assert not (home / rel).exists()


def test_copying_a_credential_file_out_is_refused(home):
    (home / ".ssh").mkdir()
    (home / ".ssh" / "id_ed25519").write_text("KEY")
    engine = SystemToolsEngine(allowed_roots=[str(home)])
    with pytest.raises(ToolError, match="protected location"):
        engine.file_operation(action="copy", source=str(home / ".ssh" / "id_ed25519"), destination=str(home / "out.txt"))


def test_protection_follows_symlinks(home, tmp_path):
    (home / "dotfiles").mkdir()
    (home / "dotfiles" / "zshrc").write_text("# real")
    (home / ".zshrc").symlink_to(home / "dotfiles" / "zshrc")
    engine = SystemToolsEngine(allowed_roots=[str(home)])
    with pytest.raises(ToolError, match="protected location"):
        engine.file_operation(action="create", source=str(home / ".zshrc"), content="evil", confirmed=True)
    assert (home / "dotfiles" / "zshrc").read_text() == "# real"


def test_ordinary_folders_that_merely_sound_similar_are_fine(home):
    engine = SystemToolsEngine(allowed_roots=[str(home)])
    for rel in ("Startup ideas/notes.txt", "projects/autostart/readme.md", "docs/ssh-tutorial.md", "my.git/notes.txt"):
        assert "Created" in engine.file_operation(action="create", source=str(home / rel), content="ok")


def test_protection_can_be_switched_off_explicitly(home):
    engine = SystemToolsEngine(allowed_roots=[str(home)], protect_sensitive_paths=False)
    assert "Created" in engine.file_operation(action="create", source=str(home / ".profile"), content="x")


# ---- 6. root guard and rename-over-existing --------------------------------------------------------------------


def test_the_allowed_root_itself_cannot_be_deleted_or_replaced(home, trash):
    engine = SystemToolsEngine(allowed_roots=[str(home)])
    with pytest.raises(ToolError, match="allowed root"):
        engine.file_operation(action="delete", source=str(home), confirmed=True)
    with pytest.raises(ToolError, match="allowed root"):
        engine.file_operation(action="rename", source=str(home), destination=str(home / "inner"), confirmed=True)
    assert not trash and (home / "a.txt").exists()


def test_rename_over_existing_warns_then_trashes_the_replaced_item(home, trash):
    engine = SystemToolsEngine(allowed_roots=[str(home)])
    with pytest.raises(ConfirmationRequired, match="already exists"):
        engine.file_operation(action="rename", source=str(home / "a.txt"), destination=str(home / "b.txt"))
    assert (home / "b.txt").read_text() == "B"
    engine.file_operation(action="rename", source=str(home / "a.txt"), destination=str(home / "b.txt"), confirmed=True)
    assert [p.name for p in trash] == ["b.txt"]
    assert (home / "b.txt").read_text() == "A" and not (home / "a.txt").exists()


def test_rename_to_a_fresh_name_keeps_the_original_prompt(home):
    engine = SystemToolsEngine(allowed_roots=[str(home)])
    with pytest.raises(ConfirmationRequired, match=r"Rename 'a.txt' to 'c.txt'\?"):
        engine.file_operation(action="rename", source=str(home / "a.txt"), destination=str(home / "c.txt"))


def test_rename_that_only_changes_case_never_trashes_the_file(home, trash):
    engine = SystemToolsEngine(allowed_roots=[str(home)])
    # same file reached through two spellings (what a case-insensitive filesystem does)
    (home / "link").symlink_to(home / "a.txt")
    engine.file_operation(action="rename", source=str(home / "a.txt"), destination=str(home / "link"), confirmed=True)
    assert not trash
