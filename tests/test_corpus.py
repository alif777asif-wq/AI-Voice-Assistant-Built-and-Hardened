"""The public corpora are pinned by checksum; tampering must be detected."""
import json
import shutil

import pytest

from harness import corpus

EXPECTED_SIZES = {"cmd": 496, "traversal": 1463, "sensitive_files": 274, "redirect": 305,
                  "apps": 7802, "paths": 5845, "odd_names": 3}


def test_manifest_verifies():
    manifest = corpus.verify_manifest()
    assert len(manifest["files"]) == 13
    assert set(manifest["sources"]) == {"payloads", "cask", "cpython", "vscode"}


def test_every_source_is_pinned_to_a_full_commit_and_has_a_licence():
    manifest = corpus.verify_manifest()
    for name, src in manifest["sources"].items():
        assert len(src["commit"]) == 40 and set(src["commit"]) <= set("0123456789abcdef"), name
        assert src["license"], name


def test_loaded_sizes_match_the_recorded_run():
    loaded = corpus.load()
    assert {k: len(v) for k, v in loaded.items()} == EXPECTED_SIZES


def test_tampering_with_one_byte_is_detected(tmp_path, monkeypatch):
    shutil.copytree(corpus.DATA, tmp_path / "data")
    target = tmp_path / "data" / "payloads" / "command-execution-unix.txt"
    raw = bytearray(target.read_bytes())
    raw[0] ^= 0x01
    target.write_bytes(bytes(raw))
    monkeypatch.setattr(corpus, "ROOT", tmp_path)
    monkeypatch.setattr(corpus, "DATA", tmp_path / "data")
    with pytest.raises(RuntimeError, match="checksum mismatch"):
        corpus.verify_manifest()


def test_a_missing_file_is_detected(tmp_path, monkeypatch):
    shutil.copytree(corpus.DATA, tmp_path / "data")
    (tmp_path / "data" / "benign" / "cpython_paths.txt").unlink()
    monkeypatch.setattr(corpus, "ROOT", tmp_path)
    monkeypatch.setattr(corpus, "DATA", tmp_path / "data")
    with pytest.raises(FileNotFoundError):
        corpus.verify_manifest()


def test_manifest_hashes_are_sha256_hex():
    manifest = json.loads((corpus.DATA / "MANIFEST.json").read_text("utf-8"))
    assert all(len(f["sha256"]) == 64 for f in manifest["files"])
