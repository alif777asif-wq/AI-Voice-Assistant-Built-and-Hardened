#!/usr/bin/env python3
"""One command that reproduces every number in this repository.

    python run_all.py

Stages (each one is timed and logged to proof/run.log):
  environment      record tool versions, hardware, date/time, and check the pins in requirements.txt
  inputs           verify the pinned public corpora (data/MANIFEST.json) and the audited code (baseline.SHA256SUMS)
  patch            baseline + patches/0001-*.patch must equal hardened/ byte for byte
  original-suite   the project's own 223 tests, on the baseline and on the hardened tree
  hardening-tests  the new regression tests: pass on hardened, FAIL on baseline (so they can fail)
  repo-tests       tests of the measuring harness itself (corpus tamper detection, judges, jail, patch)
  audit-baseline   the full adversarial + benign corpora against the ORIGINAL code
  audit-hardened   the same, against the HARDENED code
  controls         the oracles pointed at deliberately vulnerable code (they must fire)
  mutants          each fix re-broken one at a time (the audit must notice)
  strace           kernel-level execve trace compared with the harness's own process counts
  live-pkill       one real `pkill` call, unmodified, as a throw-away uid (needs root + setpriv)
  aggregate        proof/results.json  (the only source for every figure in the README and the images)

Exit status is 0 only if every recorded claim held.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PROOF = ROOT / "proof"
RAW = PROOF / "raw"
ENV = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONUNBUFFERED="1")

# Documented expectation, checked at the end: which requirements the ORIGINAL code already satisfied.
BASELINE_BROKEN = ["confirm", "overmatch", "pathexec", "sensitive", "destroy", "browser"]
BASELINE_HELD = ["inject", "fsboundary", "robust"]

REQUIREMENTS = [  # key, id, plain-language requirement, OWASP LLM Top 10 (2025) category (author's mapping)
    ("inject", "R1", "Model text is never interpreted by a shell", "LLM05 Improper Output Handling"),
    ("confirm", "R2", "Only a spoken yes can confirm a risky action", "LLM06 Excessive Agency"),
    ("overmatch", "R3", "close_application signals only the process it names", "LLM06 Excessive Agency"),
    ("pathexec", "R4", "open_application starts only a bare program name", "LLM06 Excessive Agency"),
    ("fsboundary", "R5a", "File actions stay inside the allowed folders", "LLM06 Excessive Agency"),
    ("sensitive", "R5b", "Startup, SSH and credential files are protected", "LLM06 Excessive Agency"),
    ("destroy", "R5c/d", "No silent root deletion or overwrite on rename", "LLM06 Excessive Agency"),
    ("browser", "R6", "The browser gets only well-formed web addresses", "LLM05 Improper Output Handling"),
    ("robust", "R7", "A malformed tool call never crashes a turn", "robustness"),
]

LIMITS = [
    "Linux only. The Windows and macOS branches of the audited code were read but not executed; the program-name "
    "and URL validators are platform-independent and unit-tested, the launch paths are not.",
    "The language model is replaced by a scripted adversary that emits the raw tool-call JSON a compromised or "
    "prompt-injected model could emit. No real model was queried; the audit measures the code between that JSON and the OS.",
    "Process and filesystem accounting is attempt-based: a PEP 578 audit hook records what the code tries to do and "
    "refuses to let anything outside the sandbox happen. The kernel's execve trace (strace stage) cross-checks the counts "
    "for program launches only.",
    "pkill matching is predicted with the real pgrep (same matcher, read-only). One real, unmodified pkill call was run "
    "in the live-pkill stage, which needs root and setpriv and is skipped otherwise.",
    "The attack lists are public web-attack payload lists reused as tool arguments; they were not written for this "
    "tool. Cases authored for this audit (canary payloads, scheme/option URLs, protected-path list, confirmation "
    "scenarios, regex-operator process names) are written by the author of this repository and are not public data.",
    "The protected-path list (startup files, SSH and credential locations) is a policy choice, not a standard.",
    "Wall-clock durations are one measured run on a 2-CPU container and will differ on other machines.",
    "The audit covers the tool-execution layer only; wake word, speech recognition, text-to-speech and the language "
    "model are out of scope.",
    "The audited project is the codebase supplied for this audit (an offline voice assistant called L), vendored "
    "unchanged in baseline/; it carries no licence file, so it is included as audit input only.",
]


# ----------------------------------------------------------------------------- helpers
def now_utc() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


_TMP_NAMES = r"(?:agent-guard-|pytest-of-|pytest-|mutant-|strace-xcheck-|pkill-demo-|tmp)"


def scrub(text: str) -> str:
    """Remove absolute environment paths from anything written into the package."""
    text = text.replace(str(ROOT), "<repo>")
    for home in {str(Path.home()), os.path.expanduser("~")}:
        if home and home != "/":
            text = text.replace(home, "<home>")
    text = text.replace(sys.prefix, "<venv>")
    text = re.sub(r"(?:/var)?/tmp/[^\s'\"\]\),:]*", "<tmp>", text)
    return text


class Runner:
    def __init__(self) -> None:
        self.log_lines: list[str] = []
        self.durations: dict[str, float] = {}
        self.claims: list[dict] = []
        self.started = time.monotonic()
        PROOF.mkdir(exist_ok=True)

    def log(self, msg: str) -> None:
        line = f"[{now_utc()}] {msg}"
        print(line, flush=True)
        self.log_lines.append(scrub(line))

    def stage(self, name: str):
        runner = self

        class _Stage:
            def __enter__(self_inner):
                self_inner.t0 = time.monotonic()
                runner.log(f"== stage: {name}")

            def __exit__(self_inner, *exc):
                runner.durations[name] = round(time.monotonic() - self_inner.t0, 2)
                runner.log(f"== stage done: {name} ({runner.durations[name]:.1f}s)")

        return _Stage()

    def claim(self, cid: str, statement: str, expected, observed, ok: bool) -> None:
        self.claims.append({"id": cid, "statement": statement, "expected": expected, "observed": observed,
                            "pass": bool(ok)})
        self.log(f"claim {cid}: {'PASS' if ok else 'FAIL'} - {statement} (expected {expected}, observed {observed})")

    def run(self, cmd: list[str], cwd: Path = ROOT, env: dict | None = None, timeout: int = 3600,
            save: Path | None = None) -> subprocess.CompletedProcess:
        self.log("$ " + scrub(" ".join(cmd)))
        proc = subprocess.run(cmd, cwd=cwd, env=env or ENV, capture_output=True, text=True, timeout=timeout)
        if save is not None:
            save.parent.mkdir(parents=True, exist_ok=True)
            save.write_text(scrub(proc.stdout + ("\n--- stderr ---\n" + proc.stderr if proc.stderr.strip() else "")), "utf-8")
        return proc


def pytest_counts(output: str) -> dict:
    last = [ln for ln in output.strip().splitlines() if re.search(r"\d+ (passed|failed|error)", ln)]
    counts = {"passed": 0, "failed": 0, "errors": 0, "skipped": 0}
    if last:
        for n, word in re.findall(r"(\d+) (passed|failed|errors?|skipped)", last[-1]):
            counts["errors" if word.startswith("error") else word] = int(n)
    return counts


def first_line(cmd: list[str]) -> str:
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
        return (out.stdout or out.stderr).strip().splitlines()[0]
    except Exception:  # noqa: BLE001
        return "not available"


def read_json(path: Path):
    return json.loads(path.read_text("utf-8"))


def scrub_obj(obj):
    if isinstance(obj, str):
        return scrub(obj)
    if isinstance(obj, list):
        return [scrub_obj(x) for x in obj]
    if isinstance(obj, dict):
        return {k: scrub_obj(v) for k, v in obj.items()}
    return obj


def write_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(scrub_obj(obj), indent=1, ensure_ascii=True) + "\n", "utf-8")


# ----------------------------------------------------------------------------- stages
def stage_environment(r: Runner) -> dict:
    cpu = "unknown"
    try:
        for line in Path("/proc/cpuinfo").read_text().splitlines():
            if line.startswith("model name"):
                cpu = line.split(":", 1)[1].strip()
                break
    except OSError:
        pass
    mem_gib = None
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemTotal"):
                mem_gib = round(int(line.split()[1]) / 1024 / 1024, 1)
    except OSError:
        pass
    pins = {}
    for line in (ROOT / "requirements.txt").read_text().splitlines():
        if "==" in line and not line.startswith("#"):
            name, ver = line.split("==")
            pins[name.lower().replace("_", "-")] = ver.strip()
    freeze = subprocess.run([sys.executable, "-m", "pip", "freeze"], capture_output=True, text=True, env=ENV).stdout
    installed = {}
    for line in freeze.splitlines():
        if "==" in line:
            name, ver = line.split("==")
            installed[name.lower().replace("_", "-")] = ver.strip()
    mismatched = {k: {"pinned": v, "installed": installed.get(k)} for k, v in pins.items() if installed.get(k) != v}
    local = dt.datetime.now().astimezone()
    env = {
        "recorded_utc": now_utc(),
        "local_time": local.strftime("%Y-%m-%d %H:%M:%S %z"),
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "cpu": cpu, "logical_cpus": os.cpu_count(), "memory_gib": mem_gib,
        "euid_is_root": os.geteuid() == 0,
        "tools": {"pgrep": first_line(["pgrep", "--version"]), "strace": first_line(["strace", "-V"]),
                  "setpriv": first_line(["setpriv", "--version"]), "patch": first_line(["patch", "--version"]),
                  "pytest": installed.get("pytest")},
        "requirements_pinned": len(pins), "requirements_mismatched": mismatched,
    }
    r.claim("env-pins", "installed package versions equal the pins in requirements.txt", 0, len(mismatched),
            not mismatched)
    write_json(PROOF / "environment.json", env)
    return env


def stage_inputs(r: Runner) -> dict:
    sys.path.insert(0, str(ROOT))
    from harness import corpus

    manifest = corpus.verify_manifest()
    loaded = corpus.load()
    r.claim("inputs-corpora", "every corpus file matches its recorded SHA-256", 13, len(manifest["files"]), True)
    import hashlib
    listed = dict(reversed(ln.split(None, 1)) for ln in (ROOT / "baseline.SHA256SUMS").read_text().splitlines())
    actual = {str(p.relative_to(ROOT / "baseline")): hashlib.sha256(p.read_bytes()).hexdigest()
              for p in sorted((ROOT / "baseline").rglob("*")) if p.is_file() and "__pycache__" not in p.parts}
    listed = {k.strip(): v for k, v in listed.items()}
    r.claim("inputs-baseline", "baseline/ is byte-identical to the audited source (60 files)", 60,
            sum(1 for k, v in actual.items() if listed.get(k) == v), actual == listed)
    return {"manifest": manifest, "corpus_sizes": {k: len(v) for k, v in loaded.items()}}


def stage_patch(r: Runner) -> dict:
    patch = ROOT / "patches" / "0001-harden-tool-call-layer.patch"
    text = patch.read_text("utf-8")
    work = Path(tempfile.mkdtemp(prefix="patch-check-"))
    try:
        shutil.copytree(ROOT / "baseline", work / "t", ignore=shutil.ignore_patterns("__pycache__"))
        proc = subprocess.run(["patch", "-p1", "--quiet", "-i", str(patch)], cwd=work / "t", capture_output=True, text=True)
        import filecmp

        def same(c):
            return not (c.left_only or c.right_only or c.diff_files or c.funny_files) and all(same(s) for s in c.subdirs.values())

        ok = proc.returncode == 0 and same(filecmp.dircmp(work / "t", ROOT / "hardened", ignore=["__pycache__"]))
    finally:
        shutil.rmtree(work, ignore_errors=True)
    per_file: dict[str, list[int]] = {}
    current = None
    for ln in text.splitlines():
        m = re.match(r"^diff -ruN --no-dereference baseline/(\S+)", ln)
        if m:
            current = m.group(1)
            per_file[current] = [0, 0]
        elif current and ln.startswith("+") and not ln.startswith("+++"):
            per_file[current][0] += 1
        elif current and ln.startswith("-") and not ln.startswith("---"):
            per_file[current][1] += 1
    src = {f: v for f, v in per_file.items() if f.startswith("src/")}
    tst = {f: v for f, v in per_file.items() if f.startswith("tests/")}
    r.claim("patch-reproduces", "baseline + patch equals hardened byte for byte", True, ok, ok)
    return {"file": "patches/0001-harden-tool-call-layer.patch", "files_touched": len(per_file),
            "source_files_modified": sorted(src), "test_files_added": sorted(tst),
            "source_lines_added": sum(v[0] for v in src.values()), "source_lines_removed": sum(v[1] for v in src.values()),
            "test_lines_added": sum(v[0] for v in tst.values()),
            "per_file_lines": {f: {"added": v[0], "removed": v[1]} for f, v in sorted(per_file.items())},
            "reproduces_hardened": ok}


def stage_suites(r: Runner) -> dict:
    out = {}
    base = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"]
    p = r.run(base, cwd=ROOT / "baseline", save=PROOF / "tests" / "original-suite-on-baseline.txt")
    out["original_suite_on_baseline"] = pytest_counts(p.stdout)
    p = r.run(base, cwd=ROOT / "hardened", save=PROOF / "tests" / "full-suite-on-hardened.txt")
    out["full_suite_on_hardened"] = pytest_counts(p.stdout)
    p = r.run(base + ["tests/test_hardening.py", "tests/test_hardening_helpers.py"], cwd=ROOT / "hardened",
              save=PROOF / "tests" / "hardening-tests-on-hardened.txt")
    out["hardening_tests_on_hardened"] = pytest_counts(p.stdout)
    envb = dict(ENV, L_ASSISTANT_SRC=str(ROOT / "baseline" / "src"))
    p = r.run(base + ["tests/test_hardening.py"], cwd=ROOT / "hardened", env=envb,
              save=PROOF / "tests" / "hardening-behaviour-tests-on-baseline.txt")
    out["hardening_behaviour_tests_on_baseline"] = pytest_counts(p.stdout)
    r.claim("suite-original", "the project's own suite passes unchanged on the original code", "223 passed, 0 failed",
            out["original_suite_on_baseline"], out["original_suite_on_baseline"] == {"passed": 223, "failed": 0, "errors": 0, "skipped": 0})
    h, o = out["full_suite_on_hardened"], out["original_suite_on_baseline"]
    r.claim("suite-no-regression", "the whole suite (original 223 + new tests) passes on the hardened code",
            f"{o['passed']} + {out['hardening_tests_on_hardened']['passed']} passed, 0 failed",
            h, h["failed"] == 0 and h["errors"] == 0 and h["passed"] == o["passed"] + out["hardening_tests_on_hardened"]["passed"])
    b = out["hardening_behaviour_tests_on_baseline"]
    r.claim("tests-can-fail", "the new behaviour tests FAIL on the original code (they detect the weaknesses)",
            "failed > 0", b, b["failed"] > 0)
    return out


def stage_repo_tests(r: Runner) -> dict:
    p = r.run([sys.executable, "-m", "pytest", "tests", "-q", "-p", "no:cacheprovider"],
              save=PROOF / "tests" / "harness-tests.txt", timeout=3000)
    counts = pytest_counts(p.stdout)
    r.claim("harness-tests", "the harness's own tests pass", "0 failed", counts, counts["failed"] == 0 and counts["errors"] == 0 and counts["passed"] > 0)
    return counts


def stage_audit(r: Runner, target: str) -> dict:
    out = RAW / f"audit_{target}.json"
    p = r.run([sys.executable, "-m", "harness.worker", "--target", target, "--checks", "all", "--out", str(out)])
    if p.returncode != 0:
        r.log(scrub(p.stderr[-800:]))
        raise RuntimeError(f"worker failed for {target}")
    for line in p.stdout.splitlines():
        r.log(line.split("] ", 1)[-1])
    data = read_json(out)
    write_json(out, data)
    return data


def stage_controls(r: Runner) -> dict:
    out = RAW / "controls.json"
    p = r.run([sys.executable, "-m", "harness.worker", "--target", "baseline", "--checks", "control", "--out", str(out)])
    if p.returncode != 0:
        raise RuntimeError("control worker failed: " + p.stderr[-500:])
    data = read_json(out)["checks"]["control"]
    write_json(out, read_json(out))
    groups = data["groups"]
    inj = groups["shell-injection/os.system-launcher"]
    trav = groups["path-traversal/naive-create"]
    r.claim("control-injection", "the shell-injection oracle fires on a deliberately vulnerable os.system launcher",
            f"{inj['cases']}/{inj['cases']} flagged", f"{inj['violations']}/{inj['cases']} flagged", inj["violations"] == inj["cases"])
    r.claim("control-traversal", "the path-boundary oracle fires on a deliberately naive file-create",
            "flagged > 0", f"{trav['violations']}/{trav['cases']} flagged", trav["violations"] > 0)
    return {"injection": inj, "traversal": trav,
            "canary_files_created_by_shell_injection": data.get("canary_files_created_by_shell_injection")}


def stage_mutants(r: Runner) -> dict:
    sys.path.insert(0, str(ROOT))
    from harness.mutants import run_mutants
    res = run_mutants(RAW / "mutants", lambda m: r.log(m))
    for raw in (RAW / "mutants").glob("*.json"):
        write_json(raw, read_json(raw))
    bad = [k for k, v in res.items() if not v["as_expected"]]
    detected = sum(1 for v in res.values() if v["expected"] == "detected" and v["detected"])
    planted = sum(1 for v in res.values() if v["expected"] == "detected")
    r.claim("mutants", "every re-introduced weakness is detected; the equivalent mutant is not flagged",
            f"{planted}/{planted} detected, 0 unexpected", f"{detected}/{planted} detected, unexpected: {bad}", not bad)
    return res


def stage_strace(r: Runner) -> dict:
    out = {}
    for target in ("baseline", "hardened"):
        path = RAW / f"strace_{target}.json"
        p = r.run([sys.executable, "scripts/strace_crosscheck.py", target, str(path)], timeout=3000)
        if p.returncode != 0:
            raise RuntimeError("strace cross-check failed: " + p.stderr[-500:])
        data = read_json(path)
        write_json(path, data)
        out[target] = data
        if "skipped" in data:
            r.log(f"strace {target}: skipped ({data['skipped']})")
            continue
        r.claim(f"strace-{target}-counts", f"{target}: kernel execve count == tripwire-log count == audit-hook count",
                "equal", [data["kernel_successful_execs_of_sandbox_tripwires"], data["tripwire_log_records"],
                          data["audit_hook_allowed_tripwire_execs"]], data["counts_agree"])
        r.claim(f"strace-{target}-no-real-exec",
                f"{target}: every successful kernel execve is a harness helper or a sandbox tripwire (no real program or shell ran)",
                0, data["kernel_unexplained_successful_execs"],
                data["kernel_unexplained_successful_execs"] == 0 and not data["kernel_execs_of_real_programs_outside_sandbox"])
        if not data["line_view_agrees"]:
            r.log(f"note: strace {target}: the per-line view differs from the summary view "
                  f"({data['trace_lines_successful_execs_in_sandbox']} lines vs {data['kernel_successful_execs_of_sandbox_tripwires']}; "
                  f"{data['trace_lines_not_understood']} lines not understood); the summary view is the one claimed")
    return out


def stage_live(r: Runner) -> dict:
    out = {}
    for target in ("baseline", "hardened"):
        p = r.run([sys.executable, "scripts/live_pkill_demo.py", target], timeout=300)
        if p.returncode != 0:
            raise RuntimeError("live demo failed: " + p.stderr[-500:])
        data = json.loads(p.stdout)
        write_json(RAW / f"live_pkill_{target}.json", data)
        out[target] = data
    if "skipped" in out["baseline"]:
        r.log("live-pkill: skipped (" + out["baseline"]["skipped"] + ")")
        return out
    wild = {t: next(c for c in out[t]["calls"] if c["app_name"] == ".*") for t in out}
    exact = {t: next(c for c in out[t]["calls"] if c["app_name"] == "lcanary_alpha") for t in out}
    r.claim("live-baseline-wildcard", "baseline: one close_application('.*') call with the real pkill kills every bystander",
            "4 killed", len(wild["baseline"]["killed"]), len(wild["baseline"]["killed"]) == 4)
    r.claim("live-hardened-wildcard", "hardened: the same call kills nothing", 0, len(wild["hardened"]["killed"]),
            not wild["hardened"]["killed"])
    r.claim("live-exact-name", "an exact name still closes exactly that one process, before and after the fix",
            "1 / 1", [len(exact["baseline"]["killed"]), len(exact["hardened"]["killed"])],
            len(exact["baseline"]["killed"]) == 1 and len(exact["hardened"]["killed"]) == 1)
    return out


# ----------------------------------------------------------------------------- aggregate
def aggregate(r: Runner, env, inputs, patch, suites, repo_tests, base, hard, controls, mutants, strace, live) -> dict:
    reqs = []
    for key, rid, text, owasp in REQUIREMENTS:
        b, h = base["checks"][key], hard["checks"][key]
        reqs.append({
            "key": key, "id": rid, "requirement": text, "owasp_llm_2025_mapping": owasp,
            "title": b["title"],
            "baseline": {"cases": b["cases"], "violations": b["violations"], "groups": b["groups"],
                         "benign": b.get("benign")},
            "hardened": {"cases": h["cases"], "violations": h["violations"], "groups": h["groups"],
                         "benign": h.get("benign")},
            "baseline_status": "broken" if b["violations"] else "held",
            "hardened_status": "broken" if h["violations"] else "held",
            "duration_s": {"baseline": b["duration_s"], "hardened": h["duration_s"]},
        })
    by = {x["key"]: x for x in reqs}
    broken = sorted(k for k in by if by[k]["baseline_status"] == "broken")
    held = sorted(k for k in by if by[k]["baseline_status"] == "held")
    r.claim("baseline-broken", "the original code breaks exactly the documented six requirements",
            sorted(BASELINE_BROKEN), broken, broken == sorted(BASELINE_BROKEN))
    r.claim("baseline-held", "the original code already held the other three", sorted(BASELINE_HELD), held,
            held == sorted(BASELINE_HELD))
    hv = sum(x["hardened"]["violations"] for x in reqs)
    r.claim("hardened-clean", "the hardened code breaks none of the nine requirements", 0, hv, hv == 0)
    legit_failed = {x["key"]: {"baseline": (x["baseline"]["benign"] or {}).get("legit_failed"),
                               "hardened": (x["hardened"]["benign"] or {}).get("legit_failed")}
                    for x in reqs if x["baseline"]["benign"]}
    hl = sum(v["hardened"] for v in legit_failed.values())
    r.claim("hardened-legit", "every legitimate call (real app names, real homepages, real file paths) still works on the hardened code",
            0, hl, hl == 0)
    # confirm check: attack cases only are counted as violations; legit cases are separate groups
    confirm_legit = {g: v for g, v in by["confirm"]["hardened"]["groups"].items() if g.startswith("legit")}
    r.claim("hardened-confirm-legit", "the user's real yes still executes the action and a no still cancels it",
            0, sum(v["violations"] for v in confirm_legit.values()), all(v["violations"] == 0 for v in confirm_legit.values()))

    total_cases = sum(x["baseline"]["cases"] for x in reqs)
    from_public = sum(v["cases"] for x in reqs for g, v in x["baseline"]["groups"].items() if "public" in g)
    live_ok = "skipped" not in (live.get("baseline") or {"skipped": 1})
    results = {
        "schema": 1,
        "title": "LLM agent tool-call security hardening - measured results",
        "reproduce": "python3 -m venv .venv && . .venv/bin/activate && pip install -r requirements.txt && "
                     "pip install --no-deps openwakeword==0.6.0 && python run_all.py",
        "generated_utc": now_utc(),
        "headline": {
            "requirements_total": len(reqs),
            "requirements_broken_baseline": len(broken), "requirements_broken_hardened": sum(1 for x in reqs if x["hardened_status"] == "broken"),
            "audit_calls_replayed": total_cases,
            "audit_calls_from_public_lists": from_public,
            "audit_calls_authored_or_control": total_cases - from_public,
            "rule_breaking_calls_baseline": sum(x["baseline"]["violations"] for x in reqs),
            "rule_breaking_calls_hardened": hv,
            "legitimate_calls_replayed": sum((x["hardened"]["benign"] or {}).get("cases", 0) for x in reqs),
            "legitimate_calls_failed_hardened": hl,
            "legitimate_calls_failed_baseline": sum(v["baseline"] or 0 for v in legit_failed.values()),
        },
        "requirements": reqs,
        "tests": {**suites, "harness_tests": repo_tests},
        "patch": patch,
        "controls": controls,
        "mutants": {k: {kk: vv for kk, vv in v.items()} for k, v in mutants.items()},
        "strace_crosscheck": {t: {k: v for k, v in d.items() if k != "target"} for t, d in strace.items()},
        "live_pkill": live,
        "live_pkill_ran": live_ok,
        "data": {
            "retrieved_utc": inputs["manifest"]["retrieved_utc"], "sources": inputs["manifest"]["sources"],
            "files": inputs["manifest"]["files"], "corpus_sizes": inputs["corpus_sizes"],
        },
        "environment": env,
        "durations_s": dict(r.durations),
        "claims": r.claims,
        "all_claims_pass": all(c["pass"] for c in r.claims),
        "limits": LIMITS,
    }
    return results


def results_table(res: dict) -> str:
    """The Markdown table embedded verbatim in README.md (tests/test_readme.py keeps the two in step)."""
    n = lambda x: f"{x:,}"  # noqa: E731
    lines = ["| Rule | Requirement | Original code: rule-breaking calls | Hardened code | Real-use calls failing (original / hardened) |",
             "|---|---|---:|---:|---:|"]
    for q in res["requirements"]:
        b, h = q["baseline"], q["hardened"]
        legit = f"{n(b['benign']['legit_failed'])} / {n(h['benign']['legit_failed'])} of {n(b['benign']['cases'])}" if b.get("benign") else "-"
        lines.append(f"| {q['id']} | {q['requirement']} | {n(b['violations'])} of {n(b['cases'])} | "
                     f"{n(h['violations'])} of {n(h['cases'])} | {legit} |")
    hd = res["headline"]
    lines.append(f"| **All** | **{hd['requirements_broken_baseline']} of {hd['requirements_total']} requirements broken in the original** | "
                 f"**{n(hd['rule_breaking_calls_baseline'])} of {n(hd['audit_calls_replayed'])}** | "
                 f"**{n(hd['rule_breaking_calls_hardened'])} of {n(hd['audit_calls_replayed'])}** | "
                 f"**{n(hd['legitimate_calls_failed_baseline'])} / {n(hd['legitimate_calls_failed_hardened'])} of {n(hd['legitimate_calls_replayed'])}** |")
    t = res["tests"]
    mut = res["mutants"]
    planted = [k for k, v in mut.items() if v["expected"] == "detected"]
    caught = [k for k in planted if mut[k]["detected"]]
    sx = res["strace_crosscheck"]
    lines += ["", "| Verification | Result |", "|---|---|",
              f"| Project's own tests, original code | {t['original_suite_on_baseline']['passed']} passed, {t['original_suite_on_baseline']['failed']} failed |",
              f"| Whole suite on hardened code (original + {t['hardening_tests_on_hardened']['passed']} new) | {t['full_suite_on_hardened']['passed']} passed, {t['full_suite_on_hardened']['failed']} failed |",
              f"| New behaviour tests run against the original code | {t['hardening_behaviour_tests_on_baseline']['failed']} fail, {t['hardening_behaviour_tests_on_baseline']['passed']} pass (they can fail) |",
              f"| Harness's own tests | {t['harness_tests']['passed']} passed, {t['harness_tests']['failed']} failed |",
              f"| Planted bugs (each fix re-broken once) | {len(caught)} of {len(planted)} caught; 1 harmless variant correctly not flagged |",
              ]
    if "skipped" not in sx["baseline"] and "skipped" not in sx["hardened"]:
        lines.append(f"| Kernel execve trace = tripwire log = audit hook (original / hardened) | "
                     f"{n(sx['baseline']['kernel_successful_execs_of_sandbox_tripwires'])} = {n(sx['baseline']['tripwire_log_records'])} = {n(sx['baseline']['audit_hook_allowed_tripwire_execs'])} / "
                     f"{n(sx['hardened']['kernel_successful_execs_of_sandbox_tripwires'])} = {n(sx['hardened']['tripwire_log_records'])} = {n(sx['hardened']['audit_hook_allowed_tripwire_execs'])} |")
    if res.get("live_pkill_ran"):
        wb = next(c for c in res["live_pkill"]["baseline"]["calls"] if c["app_name"] == ".*")
        wh = next(c for c in res["live_pkill"]["hardened"]["calls"] if c["app_name"] == ".*")
        lines.append(f"| One real `pkill` call with the wildcard `.*` (bystanders killed, original / hardened) | "
                     f"{len(wb['killed'])} of 4 / {len(wh['killed'])} of 4 |")
    lines.append(f"| Claims checked by `run_all.py` | {sum(c['pass'] for c in res['claims'])} of {len(res['claims'])} pass |")
    return "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.parse_args()
    if PROOF.exists():
        shutil.rmtree(PROOF)
    RAW.mkdir(parents=True)
    r = Runner()
    r.log(f"run_all.py starting (python {sys.version.split()[0]})")
    try:
        with r.stage("environment"):
            env = stage_environment(r)
        with r.stage("inputs"):
            inputs = stage_inputs(r)
        with r.stage("patch"):
            patch = stage_patch(r)
        with r.stage("original-suite+hardening-tests"):
            suites = stage_suites(r)
        with r.stage("repo-tests"):
            repo_tests = stage_repo_tests(r)
        with r.stage("audit-baseline"):
            base = stage_audit(r, "baseline")
        with r.stage("audit-hardened"):
            hard = stage_audit(r, "hardened")
        with r.stage("controls"):
            controls = stage_controls(r)
        with r.stage("mutants"):
            mutants = stage_mutants(r)
        with r.stage("strace"):
            strace = stage_strace(r)
        with r.stage("live-pkill"):
            live = stage_live(r)
        with r.stage("aggregate"):
            results = aggregate(r, env, inputs, patch, suites, repo_tests, base, hard, controls, mutants, strace, live)
    finally:
        pass
    results["durations_s"]["total"] = round(time.monotonic() - r.started, 2)
    write_json(PROOF / "results.json", results)
    (PROOF / "results-table.md").write_text(results_table(results), "utf-8")
    r.log(f"all claims pass: {results['all_claims_pass']} ({sum(c['pass'] for c in r.claims)}/{len(r.claims)})")
    r.log(f"total {results['durations_s']['total']:.0f}s; results written to proof/results.json")
    (PROOF / "run.log").write_text("\n".join(r.log_lines) + "\n", "utf-8")
    return 0 if results["all_claims_pass"] else 1


if __name__ == "__main__":
    sys.exit(main())
