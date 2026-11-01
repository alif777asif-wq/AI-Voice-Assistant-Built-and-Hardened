import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from l_assistant.tools import system_tools
from l_assistant.tools.registry import ToolDispatcher
from l_assistant.tools.system_tools import ConfirmationRequired, SystemToolsEngine, ToolError


@pytest.fixture
def sandbox(tmp_path: Path) -> Path:
    """A directory the engine below is configured to treat as its only allowed root."""
    d = tmp_path / "sandbox"
    d.mkdir()
    return d


@pytest.fixture
def engine(sandbox: Path) -> SystemToolsEngine:
    return SystemToolsEngine(allowed_roots=[str(sandbox)])


# ---- app name injection / validation -----------------------------------


@pytest.mark.parametrize(
    "bad_name",
    [
        "notepad; rm -rf /",
        "notepad && del /f /q C:\\",
        "app`whoami`",
        "app$(whoami)",
        "app|cat /etc/passwd",
        "",
        "   ",
    ],
)
def test_open_application_rejects_unsafe_names(engine: SystemToolsEngine, bad_name: str):
    with pytest.raises(ToolError):
        engine.open_application(bad_name)


def test_open_application_nonexistent_raises_tool_error(engine: SystemToolsEngine):
    with pytest.raises(ToolError):
        engine.open_application("definitely_not_a_real_binary_xyz123")


def test_close_application_no_match_returns_message_not_error(engine: SystemToolsEngine):
    result = engine.close_application("definitely_not_running_xyz123")
    assert "No running process matched" in result


# ---- path sandboxing -----------------------------------------------------


def test_file_operation_outside_allowed_roots_refused(engine: SystemToolsEngine, tmp_path: Path):
    outside = tmp_path / "outside" / "file.txt"
    with pytest.raises(ToolError, match="outside the allowed"):
        engine.file_operation(action="create", source=str(outside), content="hi")
    assert not outside.exists()


def test_file_operation_path_traversal_out_of_sandbox_refused(engine: SystemToolsEngine, sandbox: Path):
    traversal = str(sandbox / ".." / ".." / "escaped.txt")
    with pytest.raises(ToolError, match="outside the allowed"):
        engine.file_operation(action="create", source=traversal, content="hi")


def test_default_allowed_roots_is_home_directory():
    engine = SystemToolsEngine(allowed_roots=None)
    assert engine._allowed_roots == [Path.home().resolve()]


# ---- file_operation: list --------------------------------------------------


def test_list_distinguishes_folders_from_files(engine: SystemToolsEngine, sandbox: Path):
    (sandbox / "OldBackup").mkdir()
    (sandbox / "notes.txt").write_text("hi")
    result = engine.file_operation(action="list", source=str(sandbox))
    assert "1 folder(s): OldBackup" in result
    assert "1 file(s): notes.txt" in result


def test_list_resolves_the_single_folder_case(engine: SystemToolsEngine, sandbox: Path):
    # The exact scenario the system prompt guidance targets: exactly one
    # folder present, resolvable without asking the user to name it.
    (sandbox / "OnlyFolderHere").mkdir()
    result = engine.file_operation(action="list", source=str(sandbox))
    assert "1 folder(s): OnlyFolderHere" in result
    assert "file(s)" not in result  # no files present -- not mentioned at all


def test_list_multiple_folders_leaves_genuine_ambiguity_visible(engine: SystemToolsEngine, sandbox: Path):
    (sandbox / "ProjectA").mkdir()
    (sandbox / "ProjectB").mkdir()
    result = engine.file_operation(action="list", source=str(sandbox))
    assert "2 folder(s): ProjectA, ProjectB" in result


def test_list_empty_directory(engine: SystemToolsEngine, sandbox: Path):
    result = engine.file_operation(action="list", source=str(sandbox))
    assert "is empty" in result


def test_list_nonexistent_directory_raises(engine: SystemToolsEngine, sandbox: Path):
    with pytest.raises(ToolError, match="doesn't exist"):
        engine.file_operation(action="list", source=str(sandbox / "nope"))


def test_list_on_a_file_not_a_directory_raises(engine: SystemToolsEngine, sandbox: Path):
    target = sandbox / "file.txt"
    target.write_text("hi")
    with pytest.raises(ToolError, match="not a folder"):
        engine.file_operation(action="list", source=str(target))


def test_list_respects_allowed_roots_like_every_other_action(engine: SystemToolsEngine, tmp_path: Path):
    outside = tmp_path / "outside"
    outside.mkdir()
    with pytest.raises(ToolError, match="outside the allowed"):
        engine.file_operation(action="list", source=str(outside))


def test_list_never_requires_confirmation(sandbox: Path):
    # Read-only -- must work identically whether require_confirmation is
    # True or False, unlike delete/rename/overwrite.
    (sandbox / "SomeFolder").mkdir()
    strict_engine = SystemToolsEngine(allowed_roots=[str(sandbox)], require_confirmation=True)
    result = strict_engine.file_operation(action="list", source=str(sandbox))
    assert "SomeFolder" in result  # returned directly, no ConfirmationRequired


def test_list_then_delete_the_resolved_folder_still_requires_confirmation(
    engine: SystemToolsEngine, sandbox: Path
):
    # Proves list doesn't leak into or weaken the separate delete
    # confirmation gate -- resolving "the folder" via list, then
    # deleting the real name it found, still stops for confirmation
    # exactly as if the user had named it directly from the start.
    (sandbox / "OnlyFolderHere").mkdir()
    listing = engine.file_operation(action="list", source=str(sandbox))
    assert "OnlyFolderHere" in listing
    with pytest.raises(ConfirmationRequired):
        engine.file_operation(action="delete", source=str(sandbox / "OnlyFolderHere"))


# ---- destructive-action confirmation --------------------------------------


def test_delete_without_confirmation_raises_and_does_not_delete(
    engine: SystemToolsEngine, sandbox: Path
):
    target = sandbox / "important.txt"
    target.write_text("do not lose me")

    with pytest.raises(ConfirmationRequired):
        engine.file_operation(action="delete", source=str(target))

    assert target.exists()
    assert target.read_text() == "do not lose me"


def test_delete_with_confirmation_actually_deletes(engine: SystemToolsEngine, sandbox: Path):
    target = sandbox / "disposable.txt"
    target.write_text("bye")

    result = engine.file_operation(action="delete", source=str(target), confirmed=True)

    assert not target.exists()
    assert "trash" in result.lower()


def test_delete_nonexistent_file_raises_tool_error_not_confirmation(
    engine: SystemToolsEngine, sandbox: Path
):
    missing = sandbox / "does_not_exist.txt"
    with pytest.raises(ToolError):
        engine.file_operation(action="delete", source=str(missing))


def test_require_confirmation_false_skips_confirmation_for_delete(sandbox: Path):
    lenient_engine = SystemToolsEngine(allowed_roots=[str(sandbox)], require_confirmation=False)
    target = sandbox / "no_confirm_needed.txt"
    target.write_text("gone soon")

    result = lenient_engine.file_operation(action="delete", source=str(target))

    assert not target.exists()
    assert "trash" in result.lower()


def test_require_confirmation_false_skips_confirmation_for_overwrite(sandbox: Path):
    lenient_engine = SystemToolsEngine(allowed_roots=[str(sandbox)], require_confirmation=False)
    target = sandbox / "overwrite_me.txt"
    target.write_text("original")

    lenient_engine.file_operation(action="create", source=str(target), content="new content")

    assert target.read_text() == "new content"


def test_rename_without_confirmation_raises_and_does_not_rename(
    engine: SystemToolsEngine, sandbox: Path
):
    src = sandbox / "a.txt"
    dst = sandbox / "b.txt"
    src.write_text("content")

    with pytest.raises(ConfirmationRequired):
        engine.file_operation(action="rename", source=str(src), destination=str(dst))

    assert src.exists()
    assert not dst.exists()


def test_rename_with_confirmation_renames(engine: SystemToolsEngine, sandbox: Path):
    src = sandbox / "a.txt"
    dst = sandbox / "b.txt"
    src.write_text("content")

    engine.file_operation(action="rename", source=str(src), destination=str(dst), confirmed=True)

    assert not src.exists()
    assert dst.read_text() == "content"


def test_create_new_file_needs_no_confirmation(engine: SystemToolsEngine, sandbox: Path):
    target = sandbox / "new.txt"
    result = engine.file_operation(action="create", source=str(target), content="fresh")
    assert target.read_text() == "fresh"
    assert "Created" in result


def test_create_overwriting_existing_file_requires_confirmation(
    engine: SystemToolsEngine, sandbox: Path
):
    target = sandbox / "existing.txt"
    target.write_text("original")

    with pytest.raises(ConfirmationRequired):
        engine.file_operation(action="create", source=str(target), content="overwritten")

    assert target.read_text() == "original"


def test_copy_missing_destination_raises_tool_error(engine: SystemToolsEngine, sandbox: Path):
    src = sandbox / "a.txt"
    src.write_text("x")
    with pytest.raises(ToolError):
        engine.file_operation(action="copy", source=str(src))


def test_unknown_action_raises_tool_error(engine: SystemToolsEngine, sandbox: Path):
    with pytest.raises(ToolError):
        engine.file_operation(action="wipe_everything", source=str(sandbox / "x.txt"))


# ---- ToolDispatcher --------------------------------------------------------


def test_dispatcher_unknown_tool_returns_error_string_not_exception(engine: SystemToolsEngine):
    dispatcher = ToolDispatcher(engine)
    result = dispatcher.execute("delete_all_system_files", {})
    assert result.startswith("Error:")


def test_dispatcher_rejects_dunder_and_private_methods(engine: SystemToolsEngine):
    dispatcher = ToolDispatcher(engine)
    # Must not be able to reach _validate_name, __init__, etc. through the
    # same call path a legitimate tool name goes through.
    result = dispatcher.execute("__init__", {})
    assert result.startswith("Error:")
    result2 = dispatcher.execute("_check_within_allowed_roots", {})
    assert result2.startswith("Error:")


def test_dispatcher_converts_tool_error_to_string(engine: SystemToolsEngine):
    dispatcher = ToolDispatcher(engine)
    result = dispatcher.execute("open_application", {"app_name": ""})
    assert result.startswith("Error:")


def test_dispatcher_reraises_confirmation_required(engine: SystemToolsEngine, sandbox: Path):
    target = sandbox / "x.txt"
    target.write_text("y")
    dispatcher = ToolDispatcher(engine)
    with pytest.raises(ConfirmationRequired):
        dispatcher.execute("file_operation", {"action": "delete", "source": str(target)})


def test_dispatcher_bad_arguments_returns_error_string_not_traceback(engine: SystemToolsEngine):
    dispatcher = ToolDispatcher(engine)
    result = dispatcher.execute("adjust_volume", {"not_a_real_param": 1})
    assert result.startswith("Error:")


def test_search_web_default_site_opens_google(engine: SystemToolsEngine, monkeypatch):
    opened = {}
    monkeypatch.setattr("webbrowser.open", lambda url: opened.setdefault("url", url))

    result = engine.search_web("openWakeWord training")

    assert "openWakeWord" in result
    assert "google.com/search" in opened["url"]


def test_search_web_youtube_site_opens_youtube_not_google(engine: SystemToolsEngine, monkeypatch):
    # Regression coverage for the exact bug this tool replaces: "search X
    # on YouTube" must open an actual YouTube search, not a Google search
    # for "X YouTube".
    opened = {}
    monkeypatch.setattr("webbrowser.open", lambda url: opened.setdefault("url", url))

    result = engine.search_web("fifa world cup 2026", site="youtube")

    assert "fifa" in result.lower()
    assert "youtube.com/results" in opened["url"]
    assert "google.com" not in opened["url"]
    # The query itself must not have leaked the site name into it either --
    # that was the actual observed bug (query='... YouTube', tool=google).
    assert "youtube" not in opened["url"].split("search_query=")[1].lower()


def test_search_web_site_name_is_case_insensitive(engine: SystemToolsEngine, monkeypatch):
    opened = {}
    monkeypatch.setattr("webbrowser.open", lambda url: opened.setdefault("url", url))

    engine.search_web("test query", site="YouTube")

    assert "youtube.com/results" in opened["url"]


def test_search_web_unrecognized_site_falls_back_to_scoped_google_search(
    engine: SystemToolsEngine, monkeypatch
):
    opened = {}
    monkeypatch.setattr("webbrowser.open", lambda url: opened.setdefault("url", url))

    result = engine.search_web("voice assistants", site="reddit")

    assert "reddit" in result.lower()
    assert "google.com/search" in opened["url"]
    assert "site%3Areddit" in opened["url"] or "site:reddit" in opened["url"]


def test_search_web_empty_query_raises(engine: SystemToolsEngine):
    with pytest.raises(ToolError):
        engine.search_web("")


# ---- manage_browser_tabs: cross-platform key dispatch ----------------------


def test_browser_tabs_unknown_action_raises(engine: SystemToolsEngine):
    with pytest.raises(ToolError, match="Unknown browser tab action"):
        engine.manage_browser_tabs("teleport")


def test_browser_tabs_windows_dispatches_correct_combo_per_action(
    engine: SystemToolsEngine, monkeypatch
):
    monkeypatch.setattr(sys, "platform", "win32")
    calls = []
    monkeypatch.setattr(engine, "_send_key_combo_windows", lambda keys: calls.append(list(keys)))

    engine.manage_browser_tabs("new")
    engine.manage_browser_tabs("close")
    engine.manage_browser_tabs("next")
    engine.manage_browser_tabs("previous")

    assert calls == [
        ["ctrl", "t"],
        ["ctrl", "w"],
        ["ctrl", "tab"],
        ["ctrl", "shift", "tab"],
    ]


def test_browser_tabs_linux_dispatches_correct_combo_per_action(
    engine: SystemToolsEngine, monkeypatch
):
    monkeypatch.setattr(sys, "platform", "linux")
    calls = []
    monkeypatch.setattr(engine, "_send_key_combo_linux", lambda keys: calls.append(list(keys)))

    engine.manage_browser_tabs("new")
    engine.manage_browser_tabs("previous")

    assert calls == [["ctrl", "t"], ["ctrl", "shift", "tab"]]


def test_browser_tabs_linux_missing_xdotool_raises_clear_error(
    engine: SystemToolsEngine, monkeypatch
):
    monkeypatch.setattr(sys, "platform", "linux")

    def fake_run(*args, **kwargs):
        raise FileNotFoundError("xdotool not found")

    monkeypatch.setattr("subprocess.run", fake_run)

    with pytest.raises(ToolError, match="xdotool"):
        engine.manage_browser_tabs("new")


def test_browser_tabs_macos_new_and_close_work(engine: SystemToolsEngine, monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    calls = []
    monkeypatch.setattr(
        engine,
        "_send_key_combo_macos",
        lambda keystroke, modifiers: calls.append((keystroke, list(modifiers))),
    )

    engine.manage_browser_tabs("new")
    engine.manage_browser_tabs("close")

    assert calls == [("t", ["cmd"]), ("w", ["cmd"])]


def test_browser_tabs_macos_next_previous_not_supported(engine: SystemToolsEngine, monkeypatch):
    # Deliberately not supported (see system_tools.py comment): AppleScript's
    # keystroke command would type the literal string "tab" into whatever
    # has focus rather than pressing the Tab key -- failing clearly here
    # beats silently typing garbage into the user's active window.
    monkeypatch.setattr(sys, "platform", "darwin")

    with pytest.raises(ToolError, match="macOS"):
        engine.manage_browser_tabs("next")
    with pytest.raises(ToolError, match="macOS"):
        engine.manage_browser_tabs("previous")


# ---- minimize_all_windows: cross-platform dispatch -------------------------


def test_minimize_windows_uses_super_d(engine: SystemToolsEngine, monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    calls = []
    monkeypatch.setattr(engine, "_send_key_combo_windows", lambda keys: calls.append(list(keys)))

    result = engine.minimize_all_windows()

    assert calls == [["super", "d"]]
    assert "Minimized" in result


def test_minimize_linux_calls_wmctrl(engine: SystemToolsEngine, monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    calls = []

    def fake_run(args, **kwargs):
        calls.append(args)

        class Result:
            returncode = 0

        return Result()

    monkeypatch.setattr("subprocess.run", fake_run)

    result = engine.minimize_all_windows()

    assert calls == [["wmctrl", "-k", "on"]]
    assert "Minimized" in result


def test_minimize_linux_missing_wmctrl_raises_clear_error(engine: SystemToolsEngine, monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")

    def fake_run(*args, **kwargs):
        raise FileNotFoundError("wmctrl not found")

    monkeypatch.setattr("subprocess.run", fake_run)

    with pytest.raises(ToolError, match="wmctrl"):
        engine.minimize_all_windows()


def test_minimize_macos_not_supported(engine: SystemToolsEngine, monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    with pytest.raises(ToolError, match="macOS"):
        engine.minimize_all_windows()


# ---- subprocess return-code checking (the "can't open display" class of
# bug: xdotool/wmctrl running but failing, not just being absent) ----------


def test_send_key_combo_linux_raises_on_nonzero_returncode(
    engine: SystemToolsEngine, monkeypatch
):
    class FakeResult:
        returncode = 1
        stderr = "Error: Can't open display: (null)"

    monkeypatch.setattr("subprocess.run", lambda *a, **k: FakeResult())

    with pytest.raises(ToolError, match="xdotool failed"):
        engine._send_key_combo_linux(["ctrl", "t"])


def test_minimize_linux_raises_on_nonzero_returncode(engine: SystemToolsEngine, monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")

    class FakeResult:
        returncode = 1
        stderr = "Cannot open display."

    monkeypatch.setattr("subprocess.run", lambda *a, **k: FakeResult())

    with pytest.raises(ToolError, match="wmctrl failed"):
        engine.minimize_all_windows()


def test_send_key_combo_macos_raises_on_nonzero_returncode(
    engine: SystemToolsEngine, monkeypatch
):
    class FakeResult:
        returncode = 1
        stderr = "System Events got an error"

    monkeypatch.setattr("subprocess.run", lambda *a, **k: FakeResult())

    with pytest.raises(ToolError, match="osascript failed"):
        engine._send_key_combo_macos("t", ["cmd"])


# ---- open_website --------------------------------------------------------


def test_open_website_known_site_uses_curated_url(monkeypatch, sandbox: Path):
    captured = {}
    monkeypatch.setattr(system_tools.webbrowser, "open", lambda url: captured.setdefault("url", url))
    engine = SystemToolsEngine(allowed_roots=[str(sandbox)])
    result = engine.open_website(target="Facebook")
    assert captured["url"] == "https://www.facebook.com"
    assert "Facebook" in result


def test_open_website_case_and_whitespace_insensitive(monkeypatch, sandbox: Path):
    captured = {}
    monkeypatch.setattr(system_tools.webbrowser, "open", lambda url: captured.setdefault("url", url))
    engine = SystemToolsEngine(allowed_roots=[str(sandbox)])
    engine.open_website(target="  WHATSAPP  ")
    assert captured["url"] == "https://web.whatsapp.com"


def test_open_website_multiword_known_site(monkeypatch, sandbox: Path):
    captured = {}
    monkeypatch.setattr(system_tools.webbrowser, "open", lambda url: captured.setdefault("url", url))
    engine = SystemToolsEngine(allowed_roots=[str(sandbox)])
    engine.open_website(target="Claude AI")
    assert captured["url"] == "https://claude.ai"


def test_open_website_literal_domain_used_as_is(monkeypatch, sandbox: Path):
    captured = {}
    monkeypatch.setattr(system_tools.webbrowser, "open", lambda url: captured.setdefault("url", url))
    engine = SystemToolsEngine(allowed_roots=[str(sandbox)])
    engine.open_website(target="example.com")
    assert captured["url"] == "https://example.com"


def test_open_website_literal_url_with_scheme_untouched(monkeypatch, sandbox: Path):
    captured = {}
    monkeypatch.setattr(system_tools.webbrowser, "open", lambda url: captured.setdefault("url", url))
    engine = SystemToolsEngine(allowed_roots=[str(sandbox)])
    engine.open_website(target="http://example.com/some/path")
    assert captured["url"] == "http://example.com/some/path"


def test_open_website_unknown_single_word_guesses_dot_com(monkeypatch, sandbox: Path):
    captured = {}
    monkeypatch.setattr(system_tools.webbrowser, "open", lambda url: captured.setdefault("url", url))
    engine = SystemToolsEngine(allowed_roots=[str(sandbox)])
    engine.open_website(target="NotionHQ")
    assert captured["url"] == "https://notionhq.com"


def test_open_website_never_falls_back_to_a_search_url(monkeypatch, sandbox: Path):
    # The exact bug this tool exists to avoid: verify the constructed URL
    # is never a google.com/search?q=... URL for any of these inputs.
    captured = []
    monkeypatch.setattr(system_tools.webbrowser, "open", lambda url: captured.append(url))
    engine = SystemToolsEngine(allowed_roots=[str(sandbox)])
    for target in ["Facebook", "some obscure app nobody's heard of", "example.org"]:
        engine.open_website(target=target)
    assert all("search?q=" not in url for url in captured)


def test_open_website_empty_target_raises(sandbox: Path):
    engine = SystemToolsEngine(allowed_roots=[str(sandbox)])
    with pytest.raises(ToolError):
        engine.open_website(target="   ")


# ---- play_youtube_search --------------------------------------------------


class _FakeYtDlpModule:
    """Stands in for the real `yt_dlp` package -- injected into
    sys.modules so `import yt_dlp` inside play_youtube_search resolves
    to this instead. Return shape (a dict with an "entries" list, each
    entry having "id"/"title") matches what a real
    yt_dlp.YoutubeDL(...).extract_info("ytsearch1:...", ...) call
    returns with extract_flat="in_playlist" -- confirmed against a real
    yt-dlp install; only the actual network call is faked here, not the
    shape of what it returns.
    """

    class utils:
        class DownloadError(Exception):
            pass

    def __init__(self, entries=None, raise_on_extract: Exception = None):
        self._entries = entries if entries is not None else []
        self._raise_on_extract = raise_on_extract

    def YoutubeDL(self, opts):
        return _FakeYoutubeDLContext(self._entries, self._raise_on_extract)


class _FakeYoutubeDLContext:
    def __init__(self, entries, raise_on_extract):
        self._entries = entries
        self._raise_on_extract = raise_on_extract

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def extract_info(self, url, download):
        if self._raise_on_extract is not None:
            raise self._raise_on_extract
        return {"entries": self._entries}


def _install_fake_yt_dlp(monkeypatch, **kwargs):
    fake = _FakeYtDlpModule(**kwargs)
    monkeypatch.setitem(sys.modules, "yt_dlp", fake)
    return fake


def test_play_youtube_search_opens_first_result_directly(monkeypatch, sandbox: Path):
    _install_fake_yt_dlp(
        monkeypatch,
        entries=[{"id": "dQw4w9WgXcQ", "title": "Never Gonna Give You Up"}],
    )
    captured = {}
    monkeypatch.setattr(system_tools.webbrowser, "open", lambda url: captured.setdefault("url", url))

    engine = SystemToolsEngine(allowed_roots=[str(sandbox)])
    result = engine.play_youtube_search(query="never gonna give you up")

    assert captured["url"] == "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    assert "Never Gonna Give You Up" in result


def test_play_youtube_search_no_results_raises(monkeypatch, sandbox: Path):
    _install_fake_yt_dlp(monkeypatch, entries=[])
    engine = SystemToolsEngine(allowed_roots=[str(sandbox)])
    with pytest.raises(ToolError, match="No YouTube results"):
        engine.play_youtube_search(query="asdkjfhaskjdfhaskjdfh")


def test_play_youtube_search_network_error_raises_tool_error(monkeypatch, sandbox: Path):
    fake_module = _FakeYtDlpModule()
    _install_fake_yt_dlp(
        monkeypatch,
        raise_on_extract=fake_module.utils.DownloadError("network unreachable"),
    )
    engine = SystemToolsEngine(allowed_roots=[str(sandbox)])
    with pytest.raises(ToolError, match="Couldn't search YouTube"):
        engine.play_youtube_search(query="test")


def test_play_youtube_search_missing_video_id_raises(monkeypatch, sandbox: Path):
    _install_fake_yt_dlp(monkeypatch, entries=[{"title": "No ID here"}])
    engine = SystemToolsEngine(allowed_roots=[str(sandbox)])
    with pytest.raises(ToolError, match="couldn't get its video ID"):
        engine.play_youtube_search(query="test")


def test_play_youtube_search_empty_query_raises(sandbox: Path):
    engine = SystemToolsEngine(allowed_roots=[str(sandbox)])
    with pytest.raises(ToolError):
        engine.play_youtube_search(query="   ")


def test_play_youtube_search_missing_yt_dlp_gives_actionable_error(monkeypatch, sandbox: Path):
    monkeypatch.setitem(sys.modules, "yt_dlp", None)  # simulates ImportError on `import yt_dlp`
    engine = SystemToolsEngine(allowed_roots=[str(sandbox)])
    with pytest.raises(ToolError, match="pip install yt-dlp"):
        engine.play_youtube_search(query="test")


def test_play_youtube_search_only_ever_opens_a_direct_video_url(monkeypatch, sandbox: Path):
    # The exact bug this tool exists to avoid: never a /results?search_query=... page.
    _install_fake_yt_dlp(monkeypatch, entries=[{"id": "abc123", "title": "Some Video"}])
    captured = {}
    monkeypatch.setattr(system_tools.webbrowser, "open", lambda url: captured.setdefault("url", url))
    engine = SystemToolsEngine(allowed_roots=[str(sandbox)])
    engine.play_youtube_search(query="anything")
    assert "results?search_query" not in captured["url"]
    assert "/watch?v=" in captured["url"]

