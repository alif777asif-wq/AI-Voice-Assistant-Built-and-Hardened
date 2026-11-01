"""Unit tests for the new validation helpers (these names do not exist in the baseline)."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from l_assistant.tools.system_tools import ToolError, _escape_ere, _validated_web_url, _validate_program_name


def test_escape_ere_makes_every_operator_literal():
    assert _escape_ere("a.b+c") == r"a\.b\+c"
    assert _escape_ere(".*") == r"\.\*"
    assert _escape_ere("x[1](2){3}^$") == r"x\[1\]\(2\)\{3\}\^\$"




@pytest.mark.parametrize("name", [".*", ".+", "[a-z]*", "(x)", "a*", "l?", "x{2}", "^x", "x$", "a|b"])
def test_wildcard_and_regex_operator_names_are_refused(name):
    with pytest.raises(ToolError):
        _validate_program_name(name, "app_name")




@pytest.mark.parametrize("name", ["notepad", "visual studio code", "nextpad++", "firefox@nightly", "1password-cli", "Bépo layout", "node.js"])
def test_plain_application_names_are_accepted(name):
    assert _validate_program_name(name, "app_name") == name


@pytest.mark.parametrize(
    "name",
    ["/bin/sh", "./run.sh", "../x", "~/bin/x", "-h", "--gpu-launcher=x", "..", ".", ".hidden", "C:\\Windows\\cmd.exe",
     "\\\\host\\share\\x.exe", "http://example.com/x.exe", "file:///etc/passwd", "ms-msdt:/id", "_x", ""],
)
def test_paths_options_and_urls_are_refused_as_application_names(name):
    with pytest.raises(ToolError):
        _validate_program_name(name, "app_name")




@pytest.mark.parametrize(
    "url",
    ["https://www.example.com/", "http://example.com", "https://localhost:8080/x?y=1#z", "https://[::1]/", "https://münchen.de/x"],
)
def test_web_urls_are_passed_through_unchanged(url):
    assert _validated_web_url(url) == url


@pytest.mark.parametrize(
    "url",
    ["file:///home/u/.ssh/id_rsa", "javascript://a.com/%0aalert(1)", "ftp://a.com/x", "smb://h/s", "data://text/html;x.y",
     "--gpu-launcher=sh://x.y", "-new-window://x.y", "https://a.example\n--incognito", "https://exa mple.com", "https://x/\x07",
     "https:///nohost", "https://:80", "https://a..b", "//example.com", "example.com", "https://[bad"],
)
def test_non_web_or_malformed_addresses_are_refused(url):
    with pytest.raises(ToolError):
        _validated_web_url(url)


