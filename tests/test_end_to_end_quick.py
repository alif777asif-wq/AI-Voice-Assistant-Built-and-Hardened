"""Smoke-level end-to-end run on a 1-in-50 slice of every corpus: the same worker run_all.py uses."""
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
CHECKS = "pathexec,browser,sensitive,confirm,destroy"


@pytest.fixture(scope="module")
def runs(tmp_path_factory):
    out = {}
    for target in ("baseline", "hardened"):
        path = tmp_path_factory.mktemp(target) / "r.json"
        subprocess.run([sys.executable, "-m", "harness.worker", "--target", target, "--checks", CHECKS,
                        "--quick", "--out", str(path)], cwd=ROOT, check=True, capture_output=True, timeout=900)
        out[target] = json.loads(path.read_text())["checks"]
    return out


@pytest.mark.parametrize("check", CHECKS.split(","))
def test_the_original_is_flagged_and_the_hardened_tree_is_clean(runs, check):
    assert runs["baseline"][check]["violations"] > 0
    assert runs["hardened"][check]["violations"] == 0
    assert runs["baseline"][check]["cases"] == runs["hardened"][check]["cases"] > 0


@pytest.mark.parametrize("check", ["pathexec", "browser"])
def test_legitimate_calls_still_work_on_the_hardened_tree(runs, check):
    benign = runs["hardened"][check]["benign"]
    assert benign["cases"] > 0 and benign["legit_failed"] == 0
