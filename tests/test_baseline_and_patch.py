"""The audited code is byte-identical to the archive it came from, and the patch is the only difference."""
import filecmp
import hashlib
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _tracked(tree: Path):
    return sorted(p for p in tree.rglob("*") if p.is_file() and "__pycache__" not in p.parts)


def test_baseline_matches_the_recorded_checksums():
    listed = {}
    for line in (ROOT / "baseline.SHA256SUMS").read_text().splitlines():
        digest, name = line.split(None, 1)
        listed[name.strip()] = digest
    actual = {str(p.relative_to(ROOT / "baseline")): _sha(p) for p in _tracked(ROOT / "baseline")}
    assert actual == listed
    assert len(listed) == 60


def test_patch_turns_baseline_into_hardened_exactly(tmp_path):
    work = tmp_path / "tree"
    shutil.copytree(ROOT / "baseline", work, ignore=shutil.ignore_patterns("__pycache__"))
    subprocess.run(["patch", "-p1", "--quiet", "-i", str(ROOT / "patches" / "0001-harden-tool-call-layer.patch")],
                   cwd=work, check=True)
    cmp = filecmp.dircmp(work, ROOT / "hardened", ignore=["__pycache__"])

    def same(c):
        return not (c.left_only or c.right_only or c.diff_files or c.funny_files) and all(same(s) for s in c.subdirs.values())

    assert same(cmp)


def test_a_tampered_patch_no_longer_reproduces_the_hardened_tree(tmp_path):
    """The reproduction check above can fail: change one added line and the trees differ."""
    work = tmp_path / "tree"
    shutil.copytree(ROOT / "baseline", work, ignore=shutil.ignore_patterns("__pycache__"))
    text = (ROOT / "patches" / "0001-harden-tool-call-layer.patch").read_text()
    assert "confirmed is not True" in text
    (tmp_path / "bad.patch").write_text(text.replace("confirmed is not True", "confirmed is not Tru", 1))
    subprocess.run(["patch", "-p1", "--quiet", "-i", str(tmp_path / "bad.patch")], cwd=work, check=True)
    assert not filecmp.cmp(work / "src/l_assistant/tools/system_tools.py",
                           ROOT / "hardened/src/l_assistant/tools/system_tools.py", shallow=False)
