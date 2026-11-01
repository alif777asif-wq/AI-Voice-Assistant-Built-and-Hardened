# LLM agent tool-call security hardening

Repository: https://github.com/alif777asif-wq/llm-agent-tool-call-security-hardening

An independent, reproducible security audit and hardening of the tool-execution layer of an offline voice
assistant. The assistant lets a language model open applications, close processes, browse the web and change
files, so whatever can influence the model (a web page it reads, a pasted text, a prompt injection) can influence
what those tools do. This project measures how far that goes, fixes it, and proves the fix with checks that are
shown to be able to fail.

**Measured result:** the original code breaks **6 of 9** safety requirements. Of 16,738 hostile and control tool calls
replayed through the real code, **1,858** broke a rule (1,701 of them in one rule, R4). After the patch: **0**. 30,632 legitimate calls
(real application names, real web addresses, real file paths) still work: 0 fail on the hardened code.

## The problem

OWASP's Top 10 for LLM Applications (2025) lists *Improper Output Handling* (LLM05) and *Excessive Agency* (LLM06):
text produced by a model must not be trusted as a command. For an assistant with system tools this becomes
concrete questions, which this project turns into nine testable requirements:

| Rule | Requirement |
|---|---|
| R1 | Model text is never interpreted by a shell |
| R2 | Only a spoken yes can confirm a risky action |
| R3 | close_application signals only the process it names |
| R4 | open_application starts only a bare program name |
| R5a | File actions stay inside the allowed folders |
| R5b | Startup, SSH and credential files are protected |
| R5c/d | No silent root deletion or overwrite on rename |
| R6 | The browser gets only well-formed web addresses |
| R7 | A malformed tool call never crashes a turn |

## What was audited

L, the author's own offline voice assistant (wake word, speech recognition, a local
language model with tool calling, speech synthesis). It is vendored **unchanged** in `baseline/` (60 files; every
file is listed with its SHA-256 in `baseline.SHA256SUMS` and re-verified on each run; the source archive was 343,245
bytes with SHA-256 `0c959256734d6ad55c1c8864908286719ac43a9bdc553cdffda6fa1dd9d27b8e`). Only the tool layer
is in scope: `SystemToolsEngine`, `ToolDispatcher` and the orchestrator's confirmation flow.

## Approach

1. **The model is the untrusted input.** A scripted adversary emits the raw tool-call JSON a hijacked model could emit.
   Everything between that JSON and the operating system is the real code: the response parser, the orchestrator,
   the dispatcher and the engine.
2. **Tripwires and an audit hook are the judge, not the code.** Each call runs in a throw-away sandbox. `PATH` contains only
   *tripwire* programs that record their own arguments; canary files sit outside the allowed folder; a PEP 578 audit
   hook records, then vetoes, anything that would touch the real machine; the real `pgrep` predicts which processes a
   `pkill` call would signal. The code's own reply text is never used as evidence.
3. **Real inputs.** 2,538 attack strings from a public payload collection, and 13,650 real application names,
   web addresses and file paths to prove normal use still works. Every corpus file is pinned to a commit and a SHA-256.
4. **The checks must be able to fail.** Positive controls (deliberately vulnerable code), 10 planted bugs, the
   new tests run against the original code, a kernel-level `strace` cross-check and one real, unmodified `pkill` call.
5. **A small patch.** `patches/0001-harden-tool-call-layer.patch` changes 3 source files
   (+192 / -20 lines) and adds 2 test files.
   `hardened/` is `baseline/` plus that patch, byte for byte (checked on every run).

## Results

Copied from `proof/results.json` (written by `run_all.py`; `proof/results-table.md` is the same table):

| Rule | Requirement | Original code: rule-breaking calls | Hardened code | Real-use calls failing (original / hardened) |
|---|---|---:|---:|---:|
| R1 | Model text is never interpreted by a shell | 0 of 1,024 | 0 of 1,024 | - |
| R2 | Only a spoken yes can confirm a risky action | 40 of 56 | 0 of 56 | - |
| R3 | close_application signals only the process it names | 15 of 513 | 0 of 513 | 7 / 0 of 1,378 |
| R4 | open_application starts only a bare program name | 1,701 of 1,756 | 0 of 1,756 | 0 / 0 of 7,802 |
| R5a | File actions stay inside the allowed folders | 0 of 12,292 | 0 of 12,292 | 0 / 0 of 5,848 |
| R5b | Startup, SSH and credential files are protected | 41 of 60 | 0 of 60 | - |
| R5c/d | No silent root deletion or overwrite on rename | 5 of 6 | 0 of 6 | - |
| R6 | The browser gets only well-formed web addresses | 56 of 646 | 0 of 646 | 0 / 0 of 15,604 |
| R7 | A malformed tool call never crashes a turn | 0 of 385 | 0 of 385 | - |
| **All** | **6 of 9 requirements broken in the original** | **1,858 of 16,738** | **0 of 16,738** | **7 / 0 of 30,632** |

| Verification | Result |
|---|---|
| Project's own tests, original code | 223 passed, 0 failed |
| Whole suite on hardened code (original + 113 new) | 336 passed, 0 failed |
| New behaviour tests run against the original code | 54 fail, 4 pass (they can fail) |
| Harness's own tests | 31 passed, 0 failed |
| Planted bugs (each fix re-broken once) | 10 of 10 caught; 1 harmless variant correctly not flagged |
| Kernel execve trace = tripwire log = audit hook (original / hardened) | 7,882 = 7,882 = 7,882 / 7,827 = 7,827 = 7,827 |
| One real `pkill` call with the wildcard `.*` (bystanders killed, original / hardened) | 4 of 4 / 0 of 4 |
| Claims checked by `run_all.py` | 23 of 23 pass |

How to read it. A *rule-breaking call* is a call after which the recorded evidence (tripwire log, canary files, audit hook) shows attacker-chosen text reached
the system: a program was started or attempted from a model-chosen path or option, a process pattern matched more than
the named process, a file outside the allowed folder was touched, an unconfirmed destructive action ran, or a
non-web address reached the browser. Counts are attempt-based (see limits).

What the audit found in the original code:

- **R2 confirmation bypass:** 40 of 56 scenarios let a model-supplied `confirmed=true` skip the user's spoken yes (the orchestrator trusted the argument dictionary).
- **R3 over-matching `pkill`:** 15 of 17 authored process patterns signalled processes that were not named. In the live run, one `close_application(".*")` call with the real `pkill` killed 4 of 4 bystander processes and the calling process itself; the hardened code refused it (0 killed) and an exact name still closes exactly one process before and after. It also cannot close 7 real application names that contain a regular-expression character (such as `iina+`), which the hardened code can.
- **R4 paths and options as programs:** 1,701 of 1,756 hostile strings were handed to the OS as a program to execute. The kernel recorded 55 more program launches than in the hardened run (every one a sandbox tripwire); the rest were refused by the OS (non-existent paths) or vetoed by the jail (aimed at real programs).
- **R5b unprotected sensitive locations:** 41 of 60 writes or copies into startup, SSH and credential locations needed no confirmation.
- **R5c/d silent destruction:** the allowed root folder could be deleted (3 of 3) and a rename could overwrite an existing file without a trash copy (2 of 2).
- **R6 browser hand-off:** 18 of 18 authored `file:`/`javascript:`/option-style addresses and 38 of 305 public open-redirect payloads reached the browser; `search_web` (negative control) never did (0 of 323).
- **Already sound (and kept sound):** shell-metacharacter injection (R1, 0 of 1,024), folder escape (R5a, 0 of 12,292) and malformed-argument crashes (R7, 0 of 385). R1 reports zero because the original already refuses shell metacharacters; the positive control shows the same oracle fires on a deliberate `os.system` launcher (16 of 16 flagged, 10 canary files really created).

## Reproduce

Linux with Python 3.13, and the system packages `libportaudio2` (imported by the audited code), `procps` (`pgrep`)
and `patch`. `strace` and, for the live process demo, root with `setpriv` are optional: those stages are recorded as
skipped when missing.

```
python3 -m venv .venv && . .venv/bin/activate && pip install -r requirements.txt && pip install --no-deps openwakeword==0.6.0 && python run_all.py
```

`run_all.py` exits 0 only if all 23 recorded claims hold. It rewrites `proof/` (run log, `results.json`,
`results-table.md`, environment record, raw per-stage outputs, test output). Correctness figures are deterministic;
durations vary. In the recorded run on 2 CPUs / 7.8 GiB the whole run took 532 s
(audits 120 s + 109 s, planted bugs 75 s, kernel trace 182 s).
`python scripts/check_readme.py` confirms the table above is the machine-written one.

`pip install -r requirements.txt` also installs the audited assistant's own dependencies (speech and audio
libraries, about 700 MB) because its original test suite is part of the proof.

## Honest limits

- Linux only. The Windows and macOS branches of the audited code were read but not executed; the program-name and URL validators are platform-independent and unit-tested, the launch paths are not.
- The language model is replaced by a scripted adversary that emits the raw tool-call JSON a compromised or prompt-injected model could emit. No real model was queried; the audit measures the code between that JSON and the OS.
- Process and filesystem accounting is attempt-based: a PEP 578 audit hook records what the code tries to do and refuses to let anything outside the sandbox happen. The kernel's execve trace (strace stage) cross-checks the counts for program launches only.
- pkill matching is predicted with the real pgrep (same matcher, read-only). One real, unmodified pkill call was run in the live-pkill stage, which needs root and setpriv and is skipped otherwise.
- The attack lists are public web-attack payload lists reused as tool arguments; they were not written for this tool. Cases authored for this audit (canary payloads, scheme/option URLs, protected-path list, confirmation scenarios, regex-operator process names) are written by the author of this repository and are not public data.
- The protected-path list (startup files, SSH and credential locations) is a policy choice, not a standard.
- Wall-clock durations are one measured run on a 2-CPU container and will differ on other machines.
- The audit covers the tool-execution layer only; wake word, speech recognition, text-to-speech and the language model are out of scope.
- The audited project is L, the author's own offline voice assistant, vendored unchanged in baseline/ so the audit can be rerun. Its internal README (`baseline/README.md`) predates this repository's licence and is left untouched so the checksums still match.

## File map

| Path | What it is |
|---|---|
| `run_all.py` | The single reproduce command; runs every stage and writes `proof/` |
| `baseline/` | The audited code, unchanged (`baseline.SHA256SUMS` lists its checksums) |
| `hardened/` | `baseline/` + the patch, with the new tests in `hardened/tests/` |
| `patches/0001-harden-tool-call-layer.patch` | The fix; `scripts/make_patch.sh` regenerates it |
| `harness/` | The audit: sandbox, tripwires, audit-hook jail, nine checks, positive controls, planted-bug runner |
| `tests/` | Tests of the harness itself (corpus tamper detection, judges, jail, patch reproduction, end-to-end smoke run) |
| `data/` | Pinned public corpora with `MANIFEST.json` (source, licence, commit, SHA-256, retrieval date) and licence texts |
| `scripts/` | `fetch_corpora.py` (rebuild `data/` from the pinned commits), `strace_crosscheck.py`, `live_pkill_demo.py`, `make_patch.sh`, `check_readme.py` |
| `proof/` | Run log, `results.json`, `results-table.md`, `environment.json`, raw outputs, pytest output |
| `portfolio/` | The five portfolio images (HTML, PNG, JPG), generator and export scripts, entry sheet, delivery notes |
| `requirements.txt` | Pinned dependencies |

## Credits and licences

- **Attack strings:** PayloadsAllTheThings, MIT licence, commit `3ac27901c711bdf3f5b65a7b1d1820a1f65bd09a`; retrieved 2026-10-27.
- **Real application names and homepages:** Homebrew Cask, BSD-2-Clause, commit `31c8bdaf55f2af91fb7832592a548afcbba6696e`.
- **Real file paths:** CPython repository file paths only, PSF-2.0, commit `d9825d504ad738a5160d3c3ecd8cd575a1713e98`.
- **Unusual real file names:** Visual Studio Code repository file names only, MIT, commit `b8b6f56cf60c86e70fb4915cb280507013d43fba`.
  Licence texts that ship with the data are in `data/`; SHA-256 of every file is in `data/MANIFEST.json`.
- **Fonts (embedded in the portfolio images):** Fraunces, IBM Plex Sans and IBM Plex Mono, SIL Open Font License 1.1,
  via the npm packages `@fontsource/*` 5.3.0; licence texts in `portfolio/fonts/`.
- **Libraries:** pinned in `requirements.txt`; each keeps its own licence. Test tooling: pytest, pytest-asyncio; image
  export: Playwright (Chromium) and Pillow.
- **This repository** (harness, scripts, patch, portfolio, and the author's own voice-assistant code in `baseline/`
  and `hardened/`): MIT licence, see `LICENSE`.
- **Piper TTS (GPL-3.0):** L imports it for speech output. It is installed separately with `pip` and is not part of this
  repository. Anyone who ships a build that bundles Piper should check GPL-3.0 first (`baseline/README.md`, section
  "Licensing notes"). The audit itself does not need Piper to run.
