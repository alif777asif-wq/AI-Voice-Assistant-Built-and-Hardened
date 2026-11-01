#!/usr/bin/env bash
# Regenerates patches/0001-harden-tool-call-layer.patch from baseline/ -> hardened/.
# Only needed by maintainers; run_all.py verifies that baseline + patch == hardened byte for byte.
set -euo pipefail
cd "$(dirname "$0")/.."
find baseline hardened -name __pycache__ -type d -prune -exec rm -rf {} + 2>/dev/null || true
mkdir -p patches
# -N: new files appear as additions; headers carry no timestamps so the patch is deterministic.
diff -ruN --no-dereference baseline hardened | sed -E 's/^(---|\+\+\+) ([^\t]+)\t.*/\1 \2/' > patches/0001-harden-tool-call-layer.patch || true
wc -l patches/0001-harden-tool-call-layer.patch
