"""One worker process = one target (baseline | hardened | a path) x a list of checks.

Run as:  python -m harness.worker --target baseline --checks inject,overmatch --out proof/raw/baseline.json
Everything that could touch the real machine happens inside the sandbox/jail built here.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", required=True, help="baseline | hardened | path to a tree containing src/")
    ap.add_argument("--checks", default="all")
    ap.add_argument("--out", required=True)
    ap.add_argument("--quick", action="store_true", help="smoke mode: 1/50 of each corpus")
    ap.add_argument("--skip-benign", action="store_true", help="adversarial half only (used for mutants)")
    args = ap.parse_args(argv)

    sys.path.insert(0, str(ROOT))
    from harness import corpus as corpus_mod
    from harness.sandbox import Sandbox

    logging.disable(logging.CRITICAL)      # the engine logs every refusal; the raw record is in the JSON output
    t0 = time.monotonic()
    manifest = corpus_mod.verify_manifest()
    corpus = corpus_mod.load()
    if args.quick:
        corpus = {k: v[::50] for k, v in corpus.items()}
    tree = {"baseline": ROOT / "baseline", "hardened": ROOT / "hardened"}.get(args.target, Path(args.target))
    src = tree / "src"

    real_path = os.environ.get("PATH", "")
    sb = Sandbox(app_tokens=[t for t, _ in corpus["apps"]])
    os.environ.update(sb.env())           # BROWSER/HOME/PATH must be set before the engine imports webbrowser
    os.chdir(sb.home)
    sys.path.insert(0, str(src))

    from harness.jail import Jail
    from harness.runtime import Context

    import importlib
    modules = {"REAL_PATH": real_path}
    modules["system_tools"] = importlib.import_module("l_assistant.tools.system_tools")
    modules["registry"] = importlib.import_module("l_assistant.tools.registry")
    modules["llm_base"] = importlib.import_module("l_assistant.llm.base")
    for opt in ("send2trash.plat_other", "send2trash.util"):      # preload so no import happens while armed
        try:
            importlib.import_module(opt)
        except Exception:
            pass
    try:
        modules["config"] = importlib.import_module("l_assistant.config")
        modules["orchestrator"] = importlib.import_module("l_assistant.pipeline.orchestrator")
    except Exception as exc:  # heavy optional dependencies missing: confirm check falls back
        modules["orchestrator"] = None
        modules["orchestrator_error"] = f"{type(exc).__name__}: {exc}"

    jail = Jail(sb)
    sb.jail = jail
    jail.install()
    ctx = Context(args.target, sb, jail, corpus, modules)
    ctx.skip_benign = args.skip_benign

    from harness.checks import ALL, CONTROL
    registry = {**ALL, CONTROL.KEY: CONTROL}
    wanted = list(ALL) if args.checks == "all" else args.checks.split(",")
    out = {"target": args.target, "tree": str(tree.relative_to(ROOT)) if tree.is_relative_to(ROOT) else "external",
           "python": sys.version.split()[0], "started_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
           "quick": args.quick, "checks": {}, "setup_s": round(time.monotonic() - t0, 3)}
    jail.armed = True
    try:
        for key in wanted:
            mod = registry[key]
            t1 = time.monotonic()
            print(f"[{time.strftime('%H:%M:%S')}] {args.target}: running {key} ...", flush=True)
            sb.reset_home()
            result = mod.run(ctx)
            print(f"[{time.strftime('%H:%M:%S')}] {args.target}: {key} done: {result['cases']} cases, "
                  f"{result['violations']} violations, {time.monotonic() - t1:.1f}s", flush=True)
            out["checks"][key] = result
    finally:
        jail.armed = False
        # whole-run totals from three independent sources, used by the strace cross-check:
        #   the tripwire scripts' own log, the audit hook's exec events, and (outside) the kernel's execve trace
        out["totals"] = {
            "tripwire_log_records": len([r for r in sb.tripwire_log.read_bytes().decode("utf-8", "replace").split("\x1e") if r]),
            "audit_exec_events": sum(1 for e in jail.events if e["k"] == "exec"),
            "audit_exec_allowed_tripwire": sum(1 for e in jail.events if e["k"] == "exec" and e["allowed"] and jail.is_tripwire(e["resolved"])),
            "audit_exec_vetoed_real_programs": sum(1 for e in jail.events if e["k"] == "exec" and e["resolved"] is not None and not e["allowed"]),
            "audit_process_calls_vetoed": sum(1 for e in jail.events if e["k"] == "proc"),
        }
        sb.cleanup()
    out["total_s"] = round(time.monotonic() - t0, 3)
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(out, indent=1, ensure_ascii=True, default=str) + "\n", "utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
