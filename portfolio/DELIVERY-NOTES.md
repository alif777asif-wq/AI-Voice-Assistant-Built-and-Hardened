# Delivery notes

Project: llm-agent-tool-call-security-hardening. Repository URL (works only after the push command in `portfolio-entry-sheet.md` is run): https://github.com/alif777asif-wq/llm-agent-tool-call-security-hardening

## Provenance, one line per image

- **01-llm-agent-security-hardening-outcome** (Outcome): figures from `proof/results.json` fields `requirements.0.baseline_status`, `requirements.0.hardened_status`, `requirements.1.baseline_status`, `requirements.1.hardened_status`, `requirements.2.baseline_status`, `requirements.2.hardened_status`, `requirements.3.baseline_status`, `requirements.3.hardened_status`, `requirements.4.baseline_status`, `requirements.4.hardened_status`, `requirements.5.baseline_status`, `requirements.5.hardened_status`, `requirements.6.baseline_status`, `requirements.6.hardened_status`, `requirements.7.baseline_status`, `requirements.7.hardened_status`, `requirements.8.baseline_status`, `requirements.8.hardened_status`, `headline.audit_calls_replayed`, `headline.rule_breaking_calls_baseline`, `headline.rule_breaking_calls_hardened`, `headline.requirements_broken_baseline`, `headline.requirements_total`, `headline.requirements_broken_hardened`, `headline.audit_calls_authored_or_control`; built by `portfolio/build_images.py` from `proof/results.json`, which `run_all.py` wrote.
- **02-ai-agent-tool-security-requirements-task** (Task): figures from `proof/results.json` fields `requirements.0.baseline.violations`, `requirements.0.baseline.cases`, `requirements.0.hardened.violations`, `requirements.0.baseline_status`, `requirements.1.baseline.violations`, `requirements.1.baseline.cases`, `requirements.1.hardened.violations`, `requirements.1.baseline_status`, `requirements.2.baseline.violations`, `requirements.2.baseline.cases`, `requirements.2.hardened.violations`, `requirements.2.baseline_status`, `requirements.3.baseline.violations`, `requirements.3.baseline.cases`, `requirements.3.hardened.violations`, `requirements.3.baseline_status`, `requirements.4.baseline.violations`, `requirements.4.baseline.cases`, `requirements.4.hardened.violations`, `requirements.4.baseline_status`, `requirements.5.baseline.violations`, `requirements.5.baseline.cases`, `requirements.5.hardened.violations`, `requirements.5.baseline_status`, `requirements.6.baseline.violations`, `requirements.6.baseline.cases`, `requirements.6.hardened.violations`, `requirements.6.baseline_status`, `requirements.7.baseline.violations`, `requirements.7.baseline.cases`, `requirements.7.hardened.violations`, `requirements.7.baseline_status`, `requirements.8.baseline.violations`, `requirements.8.baseline.cases`, `requirements.8.hardened.violations`, `requirements.8.baseline_status`, `headline.requirements_broken_baseline`, `headline.requirements_total`, `headline.audit_calls_authored_or_control`; built by `portfolio/build_images.py` from `proof/results.json`, which `run_all.py` wrote.
- **03-llm-agent-tool-call-audit-approach** (Approach): figures from `proof/results.json` fields `patch.source_files_modified`, `patch.test_files_added`, `patch.source_lines_added`, `patch.source_lines_removed`, `tests.original_suite_on_baseline.passed`, `headline.audit_calls_authored_or_control`; built by `portfolio/build_images.py` from `proof/results.json`, which `run_all.py` wrote.
- **04-llm-agent-security-test-data-public-sources** (Data): figures from `proof/results.json` fields `data.corpus_sizes`, `data.sources`, `data.retrieved_utc`, `data.files`, `headline.audit_calls_authored_or_control`; built by `portfolio/build_images.py` from `proof/results.json`, which `run_all.py` wrote.
- **05-llm-agent-security-audit-verifiable-proof** (Proof): figures from `proof/results.json` fields `mutants`, `claims`, `all_claims_pass`, `tests.full_suite_on_hardened.passed`, `tests.original_suite_on_baseline.passed`, `strace_crosscheck.baseline.kernel_successful_execs_of_sandbox_tripwires`, `live_pkill.baseline.calls`, `live_pkill.hardened.calls`, `headline.audit_calls_authored_or_control`; built by `portfolio/build_images.py` from `proof/results.json`, which `run_all.py` wrote.

Design rationale (one sentence each):

- `01-llm-agent-security-hardening-outcome`: One oversized before/after figure (1,858 to 0) on calm document paper, with a nine-tile strip so the same result reads at thumbnail size without any small text.
- `02-ai-agent-tool-security-requirements-task`: A ruled table of the nine safety rules makes the job legible at a glance, and the single vermilion 6/9 marks what the original code broke.
- `03-llm-agent-tool-call-audit-approach`: A left-to-right pipeline whose last, dark node is the operating-system judge shows the method without a diagram library, with the five fixes on the same grid beneath it.
- `04-llm-agent-security-test-data-public-sources`: A to-scale stacked bar and a licence-and-commit table make the data provenance checkable, with 16,188 as the one dominant figure.
- `05-llm-agent-security-audit-verifiable-proof`: A dark terminal panel of claim names beside the planted-bug table, with 10/10 in the status green, turns 'trust me' into 'rerun it'.

Palette reasoning: a warm document-paper ground with near-black ink signals careful, report-style credibility to a client deciding whether the work reduces
risk; a single vermilion marks what was broken, and a status green marks what held, so colour carries meaning and is never decoration.
Typefaces: Fraunces (display and figures) and IBM Plex Sans / IBM Plex Mono (text, tables, terminal), all SIL OFL 1.1, embedded as base64 with licence texts in `portfolio/fonts/`.
Layout: 1280 x 960 artboard, 4 px baseline rhythm, a 960 x 720 core held to the central region, text columns split at the golden ratio where two columns are used.

## Omissions (what the images do not claim or show)

- No client, employer, contract, testimonial, review or user count is claimed anywhere; none exists.
- No screenshots, logos, URLs, repository paths or contact details appear in any image; the only pointer is the words "Source code linked in project".
- No wall-clock duration or speed figure appears in any image (durations vary by machine; they are in `proof/results.json` only).
- No Windows or macOS result is shown: those branches of the audited code were read but not executed.
- No result from a real language model is shown: the model is a scripted adversary, so nothing claims how often a real model would emit these calls.
- No claim that the original code was exploited anywhere in the real world; every figure is a measurement in a sandbox.
- The mapping of rules to OWASP LLM Top 10 (2025) categories is the author's classification and is labelled as such on image 2.
- The 4000 x 3000 exports are listed in `.gitignore`: they are in the zip for upload but are not committed to GitHub.

## Audit of the delivery against the brief

All figures below are read from `portfolio/measurements.json`, `portfolio/contrast.json`, `portfolio/provenance.json` and `proof/results.json`; none is typed by hand.

### Image files (every output measured)

| Image | Pixels | DPI tag | Colour profile | JPG chroma | PNG MB | JPG MB | Upload copy MB |
|---|---|---:|---|---|---:|---:|---|
| 01 | 4000 x 3000 | 300 | sRGB built-in | 4:4:4 | 0.38 | 1.36 | not needed |
| 02 | 4000 x 3000 | 300 | sRGB built-in | 4:4:4 | 0.63 | 2.02 | not needed |
| 03 | 4000 x 3000 | 300 | sRGB built-in | 4:4:4 | 0.64 | 2.05 | not needed |
| 04 | 4000 x 3000 | 300 | sRGB built-in | 4:4:4 | 0.51 | 1.64 | not needed |
| 05 | 4000 x 3000 | 300 | sRGB built-in | 4:4:4 | 0.77 | 2.14 | not needed |

Exported with Chromium 141.0.7390.37 at viewport 1280 x 960, device scale factor 3.125 (= exactly 4000 x 3000); Pillow 12.3.0, Playwright 1.56.0.

### Safe zone, central region and thumbnail legibility (measured in the rendered page)

| Image | Margins left / top / right / bottom (px of 1280 x 960) | All content within the 70 px safe zone | Headline and focal figure within the central 960 x 720 | Dominant figure at 300 px width | Headline at 300 px width |
|---|---|---|---|---|---|
| 01 | 70 / 70 / 70 / 71 | yes | yes | 46.9 px | 12.2 px |
| 02 | 70 / 70 / 70 / 71 | yes | yes | 45.0 px | 12.2 px |
| 03 | 70 / 70 / 70 / 71 | yes | yes | 25.3 px | 12.2 px |
| 04 | 70 / 70 / 70 / 71 | yes | yes | 26.2 px | 12.2 px |
| 05 | 70 / 70 / 70 / 71 | yes | yes | 26.2 px | 12.2 px |

The 300 px renders are in `portfolio/thumbnails/` and were inspected visually: the dominant figure and the headline stay legible on every image.

### Fonts

| Image | Embedded faces loaded | Families used | Intended fonts rendered |
|---|---|---|---|
| 01 | 6 of 9 | IBM Plex Sans, IBM Plex Mono, Fraunces | yes |
| 02 | 7 of 9 | IBM Plex Sans, IBM Plex Mono, Fraunces | yes |
| 03 | 7 of 9 | IBM Plex Sans, IBM Plex Mono, Fraunces | yes |
| 04 | 8 of 9 | IBM Plex Sans, IBM Plex Mono, Fraunces | yes |
| 05 | 7 of 9 | IBM Plex Sans, IBM Plex Mono, Fraunces | yes |

### Colour contrast (calculated with the WCAG formula)

| Pair | Ratio | Required | Used for |
|---|---:|---:|---|
| ink #14171C on paper #F2EFE7 | 15.63 | 7.0 | headlines, figures, table text |
| graphite #4A505A on paper #F2EFE7 | 7.07 | 4.5 | secondary text and labels |
| accent #B93319 on paper #F2EFE7 | 5.15 | 4.5 | violation counts and labels |
| ok #1B6B47 on paper #F2EFE7 | 5.64 | 4.5 | held / verified labels |
| paper #F2EFE7 on ink #14171C | 15.63 | 7.0 | terminal text, light-on-dark chips |
| ok_on_ink #8FD5AE on ink #14171C | 10.52 | 4.5 | PASS in the terminal |
| paper #F2EFE7 on accent #B93319 | 5.15 | 4.5 | text on accent chips |
| paper #F2EFE7 on ok #1B6B47 | 5.64 | 4.5 | text on status chips |
| graphite #4A505A on panel #E7E2D4 | 6.27 | 4.5 | labels on panels |
| ink #14171C on panel #E7E2D4 | 13.88 | 7.0 | text on panels |

### Source checks on the HTML

| Image | Repo URL comment is line 1 | URL occurrences (must be 1) | Static: no script, no img, no external request | No URL or contact in visible text | URL-free marker present |
|---|---|---:|---|---|---|
| 01 | yes | 1 | yes | yes | yes |
| 02 | yes | 1 | yes | yes | yes |
| 03 | yes | 1 | yes | yes | yes |
| 04 | yes | 1 | yes | yes | yes |
| 05 | yes | 1 | yes | yes | yes |

### Numbers audit (every number on the image traced to `proof/results.json`)

| Image | Distinct numbers | Unexplained |
|---|---:|---|
| 01 | 6 | none |
| 02 | 18 | none |
| 03 | 10 | none |
| 04 | 11 | none |
| 05 | 25 | none |

Unexplained numbers in total: 0.

### Reproduction and tests

`run_all.py` recorded 23 claims, 23 passing (`all_claims_pass` = True); the tests, planted bugs, kernel trace and live demo are in `proof/results.json`.
Planted-error check: 10 of 10 planted bugs detected, 1 harmless variant correctly not flagged.

## Notes on the run history

- An earlier version of the injection judge counted one baseline case as a shell launch because the basename of the string `nc -lvvp 4444 -e /bin/sh` is `sh`; the OS cannot execute such a name. The judge now ignores executions that resolve to no program, and a test (`tests/test_oracles.py`) pins that case. The original code was then re-measured at 0 of 1,024 for that rule.
- The planted bug that removes name validation while keeping the shell off is not a real vulnerability, so it is reported as an equivalent mutant that must not be flagged (and is not).
- The first clean-folder rerun of the zip found a real fault in the live process-kill demo: the throw-away user could not read the repository when it sat inside a private (mode 0700) folder, so the demo killed nothing and 2 of 23 claims failed. The demo now stages the audited code in a world-readable temporary folder, checks first that the throw-away user can import it, and records a skip with the reason if it cannot. The full proof was then rerun: 23 of 23 claims, every correctness figure identical to the earlier run (only durations and timestamps differ).
- One full run reported 2 failed legitimate launches of 7,802 on the hardened tree. They did not recur in the runs that followed. The most likely cause is the harness's wait for sandbox tripwire logs on a paused or throttled machine, which was then made robust (the give-up rule now needs both clock time and poll count); the cause was not proven, and failed legitimate calls are now recorded with details so a recurrence can be diagnosed.

## How to re-export the images

```
python portfolio/build_images.py        # rewrites portfolio/images/*.html from proof/results.json
pip install -r portfolio/requirements-export.txt && playwright install chromium
python portfolio/export_images.py       # 4000 x 3000 PNG + JPG into portfolio/exports/, writes portfolio/measurements.json
python portfolio/write_delivery_docs.py # rewrites this file and the entry sheet from the measurements
```

The audited project, L, is the author's own code, vendored unchanged in `baseline/`. The whole repository is MIT-licensed (see `LICENSE`). Piper TTS (GPL-3.0), which L imports, is installed separately with pip and is not included.
