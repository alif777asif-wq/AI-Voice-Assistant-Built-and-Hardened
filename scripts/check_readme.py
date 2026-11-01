#!/usr/bin/env python3
"""Fails unless README.md embeds, verbatim, the table that run_all.py wrote from proof/results.json.

    python scripts/check_readme.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
table = (ROOT / "proof" / "results-table.md").read_text("utf-8").strip()
readme = (ROOT / "README.md").read_text("utf-8")
if table not in readme:
    print("README.md does not contain proof/results-table.md verbatim", file=sys.stderr)
    sys.exit(1)
print("README.md contains the machine-written results table verbatim")
