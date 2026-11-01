"""Mutation check: re-introduce each fixed weakness into a copy of the hardened code and require the
matching check to catch it. A harness that cannot fail would also "pass" the hardened code."""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# (id, file under src/l_assistant, exact text to find (once), replacement, check that must catch it,
#  what was planted[, 'survive' for an equivalent mutant that must NOT be flagged])
MUTANTS = [
    ("M1-confirm", "tools/registry.py",
     "            for name in smuggled:\n                del arguments[name]\n",
     "            pass\n",
     "confirm", "dispatcher no longer strips a model-supplied 'confirmed'"),
    ("M2-overmatch", "tools/system_tools.py",
     '["pkill", "-x", "--", _escape_ere(name)],',
     '["pkill", "-x", "--", name],',
     "overmatch", "pkill pattern no longer regex-escaped"),
    ("M3-pathexec", "tools/system_tools.py",
     '    def open_application(self, app_name: str) -> str:\n        name = _validate_program_name(app_name, "app_name")',
     '    def open_application(self, app_name: str) -> str:\n        name = _validate_name(app_name, "app_name")',
     "pathexec", "open_application accepts paths and options again"),
    ("M4-browser", "tools/system_tools.py",
     '        webbrowser.open(_validated_web_url(url))\n        return f"Opening {target}."',
     '        webbrowser.open(url)\n        return f"Opening {target}."',
     "browser", "open_website hands any URL to the browser again"),
    ("M5-sensitive", "tools/system_tools.py",
     "        if not self._protect_sensitive_paths:\n            return",
     "        if True:\n            return",
     "sensitive", "protected-location check disabled"),
    ("M6a-destroy-root", "tools/system_tools.py",
     '        if self._is_allowed_root(src):\n            raise ToolError("Refusing to delete an allowed root folder itself.")\n',
     "",
     "destroy", "root-folder deletion guard removed"),
    ("M6b-destroy-overwrite", "tools/system_tools.py",
     "                self._move_to_trash(dst)\n            src.rename(dst)",
     "                pass\n            src.rename(dst)",
     "destroy", "rename no longer moves the replaced item to the trash"),
    ("M7-inject", "tools/system_tools.py",
     '        name = _validate_program_name(app_name, "app_name")\n        try:\n            if sys.platform == "win32":\n                # The safe, shell-free Windows API',
     '        name = (app_name or "").strip()\n        try:\n            if sys.platform == "win32":\n                # The safe, shell-free Windows API',
     "inject", "name validation removed from open_application but shell=False kept (equivalent mutant: "
               "metacharacters stay inert, so a precise judge must NOT flag it)", "survive"),
    ("M7b-inject-shell", "tools/system_tools.py",
     "                subprocess.Popen(\n                    [name],\n                    shell=False,",
     "                subprocess.Popen(\n                    name,\n                    shell=True,",
     "inject", "open_application launches through a shell (classic injection bug)"),
    ("M8-fsboundary", "tools/system_tools.py",
     "def _check_within_allowed_roots(path: Path, roots: List[Path]) -> None:\n    resolved = path.expanduser().resolve()",
     "def _check_within_allowed_roots(path: Path, roots: List[Path]) -> None:\n    return\n    resolved = path.expanduser().resolve()",
     "fsboundary", "allowed-root containment check disabled"),
    ("M9-robust", "tools/registry.py",
     "        except Exception as exc:  # noqa: BLE001 -- a tool must never crash a turn",
     "        except KeyboardInterrupt as exc:  # MUTATED: no catch-all",
     "robust", "dispatcher catch-all removed"),
]


def run_mutants(out_dir: Path, log) -> dict:
    results = {}
    for mid, rel, old, new, check, planted, *rest in MUTANTS:
        expect = rest[0] if rest else "detected"
        t0 = time.monotonic()
        tmp = Path(tempfile.mkdtemp(prefix="mutant-"))
        try:
            shutil.copytree(ROOT / "hardened" / "src", tmp / "src")
            path = tmp / "src" / "l_assistant" / rel
            text = path.read_text("utf-8")
            if text.count(old) != 1:
                raise RuntimeError(f"{mid}: pattern found {text.count(old)} times in {rel}")
            path.write_text(text.replace(old, new), "utf-8")
            raw = out_dir / f"mutant_{mid}.json"
            proc = subprocess.run(
                [sys.executable, "-m", "harness.worker", "--target", str(tmp), "--checks", check,
                 "--skip-benign", "--out", str(raw)],
                cwd=ROOT, capture_output=True, text=True, timeout=1800)
            if proc.returncode != 0:
                raise RuntimeError(f"{mid}: worker failed: {proc.stderr[-400:]}")
            data = json.loads(raw.read_text("utf-8"))["checks"][check]
            results[mid] = {"check": check, "planted": planted, "cases": data["cases"],
                            "violations": data["violations"], "detected": data["violations"] > 0,
                            "expected": expect,
                            "as_expected": (data["violations"] > 0) == (expect == "detected"),
                            "duration_s": round(time.monotonic() - t0, 2),
                            "first_evidence": next((r["why"][:140] for r in data["records"] if r["violation"]), "")}
            log(f"mutant {mid}: {check} -> {data['violations']} violation(s) of {data['cases']} cases "
                f"({'DETECTED' if data['violations'] else 'not flagged'}; expected: {expect})")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    return results
