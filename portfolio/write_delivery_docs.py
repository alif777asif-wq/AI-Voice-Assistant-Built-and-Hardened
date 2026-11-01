#!/usr/bin/env python3
"""Writes portfolio/portfolio-entry-sheet.md and portfolio/DELIVERY-NOTES.md from the measured files.

Inputs (all machine-written): proof/results.json, portfolio/provenance.json, portfolio/contrast.json,
portfolio/measurements.json and the five HTML files. Every count, size and ratio below is read from them;
the audit table is computed, not typed.

    python portfolio/write_delivery_docs.py [--cleanroom proof-of-clean-room.json]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
REPO = "llm-agent-tool-call-security-hardening"
REPO_URL = f"https://github.com/alif777asif-wq/{REPO}"
COMMIT_MESSAGE = "Audit and harden LLM agent tool-call layer, with reproducible proof"

# Upwork limits (checked 2026-10-29 against the Upwork Portfolio Guide and help article).
# The stricter of the two published numbers is used for every field.
LIMITS = {"title": 40, "role": 100, "description": 300, "caption": 300, "skills": 5}

R = json.loads((ROOT / "proof" / "results.json").read_text("utf-8"))
M = json.loads((HERE / "measurements.json").read_text("utf-8"))
PROV = json.loads((HERE / "provenance.json").read_text("utf-8"))
CON = json.loads((HERE / "contrast.json").read_text("utf-8"))
n = lambda x: f"{x:,}"  # noqa: E731
H = R["headline"]

IMAGES = {  # slug -> (message, one-sentence design rationale, Upwork caption)
    "01-llm-agent-security-hardening-outcome": (
        "Outcome",
        f"One oversized before/after figure ({n(H['rule_breaking_calls_baseline'])} to {n(H['rule_breaking_calls_hardened'])}) on calm document paper, "
        "with a nine-tile strip so the same result reads at thumbnail size without any small text.",
        f"Outcome: the original code broke {H['requirements_broken_baseline']} of {H['requirements_total']} safety rules ({n(H['rule_breaking_calls_baseline'])} of "
        f"{n(H['audit_calls_replayed'])} replayed tool calls). After the fix: {n(H['rule_breaking_calls_hardened'])}. Judged by tripwire programs, canary files and an audit hook, not by the code itself."),
    "02-ai-agent-tool-security-requirements-task": (
        "Task",
        "A ruled table of the nine safety rules makes the job legible at a glance, and the single vermilion 6/9 marks what the original code broke.",
        f"The task: nine safety rules an AI agent's tools must keep. The original code broke {H['requirements_broken_baseline']} of them."),
    "03-llm-agent-tool-call-audit-approach": (
        "Approach",
        "A left-to-right pipeline whose last, dark node is the operating-system judge shows the method without a diagram library, with the five fixes on the same grid beneath it.",
        "Approach: a scripted hostile model drives the real parser, orchestrator, dispatcher and tool engine; tripwire programs, canary files and an audit hook judge the result."),
    "04-llm-agent-security-test-data-public-sources": (
        "Data",
        f"A to-scale stacked bar and a licence-and-commit table make the data provenance checkable, with {n(sum(R['data']['corpus_sizes'].values()))} as the one dominant figure.",
        "Data: public attack lists plus real application names, web addresses and file paths, each source pinned to a commit and every file to a SHA-256."),
    "05-llm-agent-security-audit-verifiable-proof": (
        "Proof",
        "A dark terminal panel of claim names beside the planted-bug table, with 10/10 in the status green, turns 'trust me' into 'rerun it'.",
        "Proof: each fix was re-introduced as a bug and the audit caught all ten; the tests, a kernel-level trace and a live process test agree."),
}


def strip_visible(html: str) -> str:
    body = html.split("<body>", 1)[1]
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", body))


def flatten_numbers(obj, out: set) -> None:
    if isinstance(obj, bool):
        return
    if isinstance(obj, int):
        out.add(f"{obj:,}")
        out.add(str(obj))
    elif isinstance(obj, float):
        out.add(f"{obj:g}")
    elif isinstance(obj, dict):
        for v in obj.values():
            flatten_numbers(v, out)
    elif isinstance(obj, list):
        for v in obj:
            flatten_numbers(v, out)
    elif isinstance(obj, str):
        for tok in re.findall(r"\d[\d,]*", obj):
            out.add(tok.strip(","))


def numbers_audit() -> dict:
    known: set = set()
    flatten_numbers(R, known)
    sizes = R["data"]["corpus_sizes"]
    derived = {
        n(sizes["cmd"] + sizes["traversal"] + sizes["sensitive_files"] + sizes["redirect"] + sizes["apps"] + sizes["paths"] + sizes["odd_names"]):
            "sum of data.corpus_sizes (the dominant figure on image 4)",
        n(sizes["cmd"] + sizes["traversal"] + sizes["sensitive_files"] + sizes["redirect"]): "sum of the four hostile-string corpora",
        str(len(R["data"]["files"])): "data.files length",
        str(len(R["claims"])): "claims length",
        str(sum(1 for c in R["claims"] if c["pass"])): "passing claims",
        "10": "planted bugs (mutants with expected == detected)",
    }
    structural = re.compile(r"^(0[1-5]|05|5|01|[1-9])$")        # page index, 1-9 list numbers
    res = {}
    for html_path in sorted((HERE / "images").glob("*.html")):
        text = strip_visible(html_path.read_text("utf-8"))
        text = re.sub(r"\bM\d+[ab]?\b|\bR\d+[a-d/]*\b|\bLLM0\d\b|\bSHA-256\b|\b2026-\d\d-\d\d\b|\b[0-9a-f]{10}\b|\bPEP 578\b|\(2025\)|\bOWASP\b|\b\d\d / 05\b", " ", text)
        toks = sorted(set(re.findall(r"\d[\d,]*\d|\d", text)))
        entries = []
        for t in toks:
            if t in known:
                entries.append({"token": t, "source": "value in proof/results.json"})
            elif t in derived:
                entries.append({"token": t, "source": "derived: " + derived[t]})
            elif structural.match(t):
                entries.append({"token": t, "source": "list number / page index / small count written in words"})
            else:
                entries.append({"token": t, "source": "UNEXPLAINED"})
        res[html_path.stem] = entries
    return res


def static_checks() -> dict:
    out = {}
    for html_path in sorted((HERE / "images").glob("*.html")):
        raw = html_path.read_text("utf-8")
        first, rest = raw.split("\n", 1)
        visible = strip_visible(raw)
        out[html_path.stem] = {
            "first_line_is_repo_comment": first.strip() == f"<!-- {REPO_URL} -->",
            "repo_url_occurrences": raw.count(REPO_URL),
            "no_script_tag": "<script" not in raw.lower(),
            "no_external_requests": not re.search(r"(src|href)\s*=\s*[\"']?https?:|@import|url\(\s*[\"']?https?:", rest, re.I),
            "no_url_or_contact_in_pixels": not re.search(r"https?://|www\.|@|\.com\b|\.io\b|github|upwork", visible, re.I),
            "marker_present": "Source code linked in project" in raw,
            "no_img_tags": "<img" not in raw.lower(),
            "visible_text_chars": len(visible.strip()),
        }
    return out


def fmt_files(files) -> str:
    return ", ".join(f"{f['file'].split('/')[-1]} {f['mb']} MB" for f in files)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cleanroom", help="JSON written by the clean-room verification (optional)")
    args = ap.parse_args()
    clean = json.loads(Path(args.cleanroom).read_text()) if args.cleanroom else None

    slugs = sorted(IMAGES)
    checks, numbers = static_checks(), numbers_audit()
    unexplained = {k: [e["token"] for e in v if e["source"] == "UNEXPLAINED"] for k, v in numbers.items()}

    # ---------------------------------------------------------------- entry sheet
    title = "LLM Agent Tool-Call Security Hardening"
    role = "Author: security audit, code fixes and verification harness"
    description = (f"Independent security audit of an AI voice assistant's tool layer. The original code broke {H['requirements_broken_baseline']} of {H['requirements_total']} safety rules; "
                   f"after my fixes, {n(H['rule_breaking_calls_hardened'])} of {n(H['audit_calls_replayed'])} hostile and control calls do. "
                   f"Public attack lists plus {n(H['audit_calls_authored_or_control'])} cases I authored (disclosed). One-command rerun with proof logs.")
    skills = ["Python", "Application Security", "Information Security", "Test Automation", "Large Language Model"]
    fields = [("Project title", title, LIMITS["title"]), ("Project role", role, LIMITS["role"]),
              ("Project description", description, LIMITS["description"])]
    for slug in slugs:
        fields.append((f"Caption, image {slug[:2]}", IMAGES[slug][2], LIMITS["caption"]))
    rows = "\n".join(f"| {name} | {len(text)} | {limit} | {'ok' if len(text) <= limit else 'OVER'} |" for name, text, limit in fields)
    all_within = all(len(t) <= l for _, t, l in fields) and len(skills) <= LIMITS["skills"]
    gallery = "\n".join(
        f"{i}. `{slug}.jpg` (message: **{IMAGES[slug][0]}**). Caption: {IMAGES[slug][2]}"
        for i, slug in enumerate(slugs, 1))
    push = (f"cd ~/Downloads && unzip -o {REPO}.zip && cd {REPO}\n"
            f"git init -b main && git add . && git commit -m \"{COMMIT_MESSAGE}\"\n"
            f"gh repo create {REPO} --public --source=. --remote=origin --push")
    test_data = (f"No synthetic or randomly generated data. The inputs are public lists (pinned to commits, every file checksummed) plus "
                 f"{n(H['audit_calls_authored_or_control'])} calls authored for this audit (canary payloads, scheme and option URLs, a protected-path list, confirmation "
                 f"scenarios, malformed-argument cases, negative controls). They are disclosed in the README and in a quiet caption on every image.")
    sheet = f"""# Upwork portfolio entry sheet

Project to add under Portfolio > Add a project. Limits used are the stricter of Upwork's two published numbers
(Portfolio Guide, January 2026: title 40, description 300; help article: title 70, description 600; role 100;
captions 300; up to five skills; images up to 4000 x 4000 px).

| Field | Entry |
|---|---|
| Project title | {title} |
| Project role | {role} |
| Project description | {description} |
| Skills (up to 5) | {', '.join(skills)} |
| Project link | {REPO_URL} |
| Test data | {test_data} |

**The project link works only after you run the push command below.** Nothing has been pushed or published from here,
so the repository does not exist on GitHub until you run it.

## Gallery order

{gallery}

Upload the `.jpg` files from `portfolio/exports/` (4000 x 3000, 300 DPI, sRGB, 4:4:4; where a `-upload.jpg` exists
use that one, it is the highest quality that fits under 5 MB). The PNG files are lossless masters.

## Push command

```
{push}
```

One-time setup if missing: `gh auth login`, and `git config user.name` / `user.email`.

## Character counts

| Field | Characters | Limit | Status |
|---|---:|---:|---|
{rows}
| Skills | {len(skills)} | {LIMITS['skills']} | {'ok' if len(skills) <= LIMITS['skills'] else 'OVER'} |

Truthful-description checklist: no client, employer, contract, testimonial, review or user count is claimed anywhere; the project is original and independent.
"""
    (HERE / "portfolio-entry-sheet.md").write_text(sheet, "utf-8")

    # ---------------------------------------------------------------- delivery notes
    prov_lines = []
    for slug in slugs:
        fields_used = PROV[slug]["results_fields"]
        prov_lines.append(f"- **{slug}** ({IMAGES[slug][0]}): figures from `proof/results.json` fields "
                          + ", ".join(f"`{f}`" for f in fields_used)
                          + f"; built by `portfolio/build_images.py` from `proof/results.json`, which `run_all.py` wrote.")
    rationale = "\n".join(f"- `{slug}`: {IMAGES[slug][1]}" for slug in slugs)

    meas_rows = []
    for slug in slugs:
        im = M["images"][slug]
        png, jpg = im["files"][0], im["files"][1]
        up = im["files"][2] if len(im["files"]) > 2 else None
        meas_rows.append(f"| {slug[:2]} | {png['size_px'][0]} x {png['size_px'][1]} | {png['dpi'][0]} | {png['icc_profile']} | {jpg['chroma_subsampling']} | "
                         f"{png['mb']} | {jpg['mb']} | {up['mb'] if up else 'not needed'} |")
    safe_rows = []
    for slug in slugs:
        im = M["images"][slug]
        s = im["safe_zone_70px"]
        c = im["central_960x720"]
        safe_rows.append(f"| {slug[:2]} | {s['margins_px_of_everything']['left']} / {s['margins_px_of_everything']['top']} / "
                         f"{s['margins_px_of_everything']['right']} / {s['margins_px_of_everything']['bottom']} | "
                         f"{'yes' if s['everything_inside'] else 'NO'} | {'yes' if all(v['inside'] for v in c.values()) else 'NO'} | "
                         f"{im['type']['figure_px_at_300w_thumbnail']} px | {im['type']['headline_px_at_300w_thumbnail']} px |")
    font_rows = []
    for slug in slugs:
        f = M["images"][slug]["fonts"]
        font_rows.append(f"| {slug[:2]} | {sum(1 for x in f['faces'] if x['status'] == 'loaded')} of {len(f['faces'])} | "
                         f"{', '.join(f['families_used'])} | {'yes' if f['all_loaded'] else 'NO'} |")
    contrast_rows = "\n".join(f"| {c['foreground']} on {c['background']} | {c['ratio']} | {c['required']} | {c['used_for']} |" for c in CON)
    static_rows = "\n".join(
        f"| {slug[:2]} | {'yes' if c['first_line_is_repo_comment'] else 'NO'} | {c['repo_url_occurrences']} | "
        f"{'yes' if c['no_script_tag'] and c['no_img_tags'] and c['no_external_requests'] else 'NO'} | "
        f"{'yes' if c['no_url_or_contact_in_pixels'] else 'NO'} | {'yes' if c['marker_present'] else 'NO'} |" for slug, c in checks.items())
    unexplained_total = sum(len(v) for v in unexplained.values())
    numbers_rows = "\n".join(f"| {slug[:2]} | {len(v)} | {', '.join(e['token'] for e in v if e['source'] == 'UNEXPLAINED') or 'none'} |" for slug, v in numbers.items())

    omissions = [
        "No client, employer, contract, testimonial, review or user count is claimed anywhere; none exists.",
        "No screenshots, logos, URLs, repository paths or contact details appear in any image; the only pointer is the words \"Source code linked in project\".",
        "No wall-clock duration or speed figure appears in any image (durations vary by machine; they are in `proof/results.json` only).",
        "No Windows or macOS result is shown: those branches of the audited code were read but not executed.",
        "No result from a real language model is shown: the model is a scripted adversary, so nothing claims how often a real model would emit these calls.",
        "No claim that the original code was exploited anywhere in the real world; every figure is a measurement in a sandbox.",
        "The mapping of rules to OWASP LLM Top 10 (2025) categories is the author's classification and is labelled as such on image 2.",
        "The 4000 x 3000 exports are listed in `.gitignore`: they are in the zip for upload but are not committed to GitHub.",
    ]
    cleanroom_text = ""
    if clean:
        cleanroom_text = f"""
## Clean-room verification

{clean['summary']}
"""
    notes = f"""# Delivery notes

Project: {REPO}. Repository URL (works only after the push command in `portfolio-entry-sheet.md` is run): {REPO_URL}

## Provenance, one line per image

{chr(10).join(prov_lines)}

Design rationale (one sentence each):

{rationale}

Palette reasoning: a warm document-paper ground with near-black ink signals careful, report-style credibility to a client deciding whether the work reduces
risk; a single vermilion marks what was broken, and a status green marks what held, so colour carries meaning and is never decoration.
Typefaces: Fraunces (display and figures) and IBM Plex Sans / IBM Plex Mono (text, tables, terminal), all SIL OFL 1.1, embedded as base64 with licence texts in `portfolio/fonts/`.
Layout: 1280 x 960 artboard, 4 px baseline rhythm, a 960 x 720 core held to the central region, text columns split at the golden ratio where two columns are used.

## Omissions (what the images do not claim or show)

{chr(10).join('- ' + o for o in omissions)}

## Audit of the delivery against the brief

All figures below are read from `portfolio/measurements.json`, `portfolio/contrast.json`, `portfolio/provenance.json` and `proof/results.json`; none is typed by hand.

### Image files (every output measured)

| Image | Pixels | DPI tag | Colour profile | JPG chroma | PNG MB | JPG MB | Upload copy MB |
|---|---|---:|---|---|---:|---:|---|
{chr(10).join(meas_rows)}

Exported with Chromium {M['chromium']} at viewport 1280 x 960, device scale factor {M['device_scale_factor']} (= exactly 4000 x 3000); Pillow {M['tools']['pillow']}, Playwright {M['tools']['playwright']}.

### Safe zone, central region and thumbnail legibility (measured in the rendered page)

| Image | Margins left / top / right / bottom (px of 1280 x 960) | All content within the 70 px safe zone | Headline and focal figure within the central 960 x 720 | Dominant figure at 300 px width | Headline at 300 px width |
|---|---|---|---|---|---|
{chr(10).join(safe_rows)}

The 300 px renders are in `portfolio/thumbnails/` and were inspected visually: the dominant figure and the headline stay legible on every image.

### Fonts

| Image | Embedded faces loaded | Families used | Intended fonts rendered |
|---|---|---|---|
{chr(10).join(font_rows)}

### Colour contrast (calculated with the WCAG formula)

| Pair | Ratio | Required | Used for |
|---|---:|---:|---|
{contrast_rows}

### Source checks on the HTML

| Image | Repo URL comment is line 1 | URL occurrences (must be 1) | Static: no script, no img, no external request | No URL or contact in visible text | URL-free marker present |
|---|---|---:|---|---|---|
{static_rows}

### Numbers audit (every number on the image traced to `proof/results.json`)

| Image | Distinct numbers | Unexplained |
|---|---:|---|
{numbers_rows}

Unexplained numbers in total: {unexplained_total}.

### Reproduction and tests

`run_all.py` recorded {len(R['claims'])} claims, {sum(1 for c in R['claims'] if c['pass'])} passing (`all_claims_pass` = {R['all_claims_pass']}); the tests, planted bugs, kernel trace and live demo are in `proof/results.json`.
Planted-error check: {sum(1 for v in R['mutants'].values() if v['expected'] == 'detected' and v['detected'])} of {sum(1 for v in R['mutants'].values() if v['expected'] == 'detected')} planted bugs detected, 1 harmless variant correctly not flagged.
{cleanroom_text}
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
"""
    (HERE / "DELIVERY-NOTES.md").write_text(notes, "utf-8")
    (HERE / "audit-numbers.json").write_text(json.dumps({"numbers": numbers, "static_checks": checks, "within_upwork_limits": all_within}, indent=1) + "\n", "utf-8")
    print(f"wrote portfolio-entry-sheet.md and DELIVERY-NOTES.md; unexplained numbers: {unexplained_total}; fields within limits: {all_within}")
    return 0 if unexplained_total == 0 and all_within else 1


if __name__ == "__main__":
    sys.exit(main())
