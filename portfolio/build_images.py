#!/usr/bin/env python3
"""Builds the five portfolio images (self-contained HTML + CSS, no JavaScript, no external files).

Every figure on every image is read from proof/results.json through Page.v("dotted.path"); the paths each
image used are written to portfolio/provenance.json. Fonts are embedded as base64 @font-face (SIL OFL 1.1,
licence texts in portfolio/fonts/). Colour contrast is calculated here and written to portfolio/contrast.json.

    python portfolio/build_images.py
"""
from __future__ import annotations

import base64
import html
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
OUT = HERE / "images"
REPO_URL = "https://github.com/alif777asif-wq/llm-agent-tool-call-security-hardening"   # HTML comment only, never pixels

PALETTE = {
    "paper": "#F2EFE7",      # warm document paper: calm, credible, "report" rather than "app"
    "panel": "#E7E2D4",
    "ink": "#14171C",
    "graphite": "#4A505A",
    "hair": "#C5BFAE",
    "accent": "#B93319",     # the one decisive accent: a violation / a stop sign
    "ok": "#1B6B47",         # status colour only: a requirement that held
    "ok_on_ink": "#8FD5AE",  # the same status colour on the dark terminal panel
}

# (foreground, background, minimum ratio, what it colours)
CONTRAST_PAIRS = [
    ("ink", "paper", 7.0, "headlines, figures, table text"),
    ("graphite", "paper", 4.5, "secondary text and labels"),
    ("accent", "paper", 4.5, "violation counts and labels"),
    ("ok", "paper", 4.5, "held / verified labels"),
    ("paper", "ink", 7.0, "terminal text, light-on-dark chips"),
    ("ok_on_ink", "ink", 4.5, "PASS in the terminal"),
    ("paper", "accent", 4.5, "text on accent chips"),
    ("paper", "ok", 4.5, "text on status chips"),
    ("graphite", "panel", 4.5, "labels on panels"),
    ("ink", "panel", 7.0, "text on panels"),
]

FONT_FILES = [
    ("Fraunces", 400, "fraunces-latin-400-normal.woff2"), ("Fraunces", 600, "fraunces-latin-600-normal.woff2"),
    ("Fraunces", 800, "fraunces-latin-800-normal.woff2"),
    ("IBM Plex Sans", 400, "ibm-plex-sans-latin-400-normal.woff2"), ("IBM Plex Sans", 500, "ibm-plex-sans-latin-500-normal.woff2"),
    ("IBM Plex Sans", 600, "ibm-plex-sans-latin-600-normal.woff2"),
    ("IBM Plex Mono", 400, "ibm-plex-mono-latin-400-normal.woff2"), ("IBM Plex Mono", 500, "ibm-plex-mono-latin-500-normal.woff2"),
    ("IBM Plex Mono", 600, "ibm-plex-mono-latin-600-normal.woff2"),
]


# ------------------------------------------------------------------ colour maths
def _lin(c: float) -> float:
    c /= 255
    return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4


def luminance(hex_colour: str) -> float:
    r, g, b = (int(hex_colour[i:i + 2], 16) for i in (1, 3, 5))
    return 0.2126 * _lin(r) + 0.7152 * _lin(g) + 0.0722 * _lin(b)


def contrast(fg: str, bg: str) -> float:
    a, b = luminance(PALETTE[fg]), luminance(PALETTE[bg])
    hi, lo = max(a, b), min(a, b)
    return (hi + 0.05) / (lo + 0.05)


# ------------------------------------------------------------------ data access
RESULTS = json.loads((ROOT / "proof" / "results.json").read_text("utf-8"))


def n(x) -> str:
    return f"{x:,}"


def esc(s) -> str:
    return html.escape(str(s), quote=True)


class Page:
    def __init__(self, idx: int, slug: str, rail: str, title: str) -> None:
        self.idx, self.slug, self.rail, self.title = idx, slug, rail, title
        self.used: list[str] = []

    def v(self, path: str, fmt=n):
        cur = RESULTS
        for part in path.split("."):
            cur = cur[int(part)] if isinstance(cur, list) else cur[part]
        if path not in self.used:
            self.used.append(path)
        return fmt(cur) if fmt else cur


def short_risk(label: str) -> str:
    names = {"LLM05": "Output handling", "LLM06": "Excessive agency"}
    code = label.split()[0]
    return f"{code} \u00b7 {names[code]}" if code in names else label.capitalize()


def req(key: str) -> dict:
    return next(r for r in RESULTS["requirements"] if r["key"] == key)


def req_index(key: str) -> int:
    return next(i for i, r in enumerate(RESULTS["requirements"]) if r["key"] == key)


# ------------------------------------------------------------------ shared CSS
def font_css() -> str:
    faces = []
    for family, weight, fname in FONT_FILES:
        b64 = base64.b64encode((HERE / "fonts" / fname).read_bytes()).decode("ascii")
        faces.append(f'@font-face{{font-family:"{family}";font-weight:{weight};font-style:normal;font-display:block;'
                     f'src:url(data:font/woff2;base64,{b64}) format("woff2")}}')
    return "\n".join(faces)


BASE_CSS = """
:root{--paper:@paper;--panel:@panel;--ink:@ink;--graphite:@graphite;--hair:@hair;--accent:@accent;--ok:@ok;--ok-on-ink:@ok_on_ink;
--display:"Fraunces",Georgia,"Times New Roman",serif;--sans:"IBM Plex Sans","Helvetica Neue",Arial,sans-serif;--mono:"IBM Plex Mono","SFMono-Regular",Consolas,monospace}
*{box-sizing:border-box;margin:0;padding:0}
html,body{width:1280px;height:960px;background:var(--paper);color:var(--ink);overflow:hidden}
body{font-family:var(--sans);font-size:16px;line-height:24px;font-variant-numeric:tabular-nums lining-nums;font-feature-settings:"tnum" 1,"lnum" 1;-webkit-font-smoothing:antialiased;text-rendering:geometricPrecision}
.art{position:relative;width:1280px;height:960px;background:var(--paper)}
.lab{font-family:var(--mono);font-size:12px;line-height:16px;letter-spacing:.09em;text-transform:uppercase;color:var(--graphite);font-weight:500}
.top{position:absolute;left:70px;top:70px;width:1140px;height:32px;border-bottom:1px solid var(--hair);display:flex;justify-content:space-between;align-items:flex-start}
.bottom{position:absolute;left:70px;top:858px;width:1140px;height:32px;border-top:1px solid var(--hair);display:flex;justify-content:space-between;align-items:flex-end}
.bottom .cap{font-family:var(--sans);font-size:12px;line-height:16px;color:var(--graphite);max-width:760px}
.bottom .mark{font-family:var(--mono);font-size:12px;line-height:16px;letter-spacing:.09em;text-transform:uppercase;color:var(--ink);font-weight:600}
.rail{position:absolute;left:70px;top:120px;width:24px;height:720px;border-right:1px solid var(--hair)}
.rail span{position:absolute;left:0;top:0;width:24px;height:720px;writing-mode:vertical-rl;transform:rotate(180deg);text-align:left;font-family:var(--mono);font-size:12px;line-height:24px;letter-spacing:.18em;text-transform:uppercase;color:var(--graphite);font-weight:500}
.core{position:absolute;left:160px;top:120px;width:960px;height:720px}
h1{font-family:var(--display);font-weight:800;font-size:52px;line-height:56px;letter-spacing:-.012em;color:var(--ink)}
.sub{font-family:var(--sans);font-weight:500;font-size:20px;line-height:28px;color:var(--graphite)}
.sub b{color:var(--ink);font-weight:600}
.fig{font-family:var(--display);font-weight:800;line-height:1;letter-spacing:-.02em;font-variant-numeric:tabular-nums lining-nums;font-feature-settings:"tnum" 1,"lnum" 1}
.red{color:var(--accent)}.grn{color:var(--ok)}.gr{color:var(--graphite)}
.sq{display:inline-block;width:12px;height:12px;vertical-align:-1px;margin-right:8px}
.sq.bad{background:var(--accent)}.sq.good{background:var(--ok)}.sq.neutral{border:2px solid var(--graphite)}
.arr{position:relative;display:inline-block;color:var(--graphite)}
.arr::before{content:"";position:absolute;left:0;right:20px;top:50%;height:4px;margin-top:-2px;background:currentColor}
.arr::after{content:"";position:absolute;right:0;top:50%;margin-top:-14px;border-left:24px solid currentColor;border-top:14px solid transparent;border-bottom:14px solid transparent}
.hr{border:0;border-top:1px solid var(--hair)}
"""


def page_html(p: Page, extra_css: str, body: str, caption: str) -> str:
    css = BASE_CSS
    for k, val in PALETTE.items():
        css = css.replace("@" + k, val)
    top = ("LLM agent security · Tool-call audit")
    return f"""<!-- {REPO_URL} -->
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=1280">
<title>{esc(p.title)}</title>
<style>
{font_css()}
{css}
{extra_css}
</style>
</head>
<body>
<div class="art">
<header class="top"><span class="lab">{esc(top)}</span><span class="lab">{p.idx:02d} / 05</span></header>
<aside class="rail"><span>{esc(p.rail)}</span></aside>
<main class="core">
{body}
</main>
<footer class="bottom"><span class="cap">{esc(caption)}</span><span class="mark">Source code linked in project</span></footer>
</div>
</body>
</html>
"""


def caption_text(p: Page) -> str:
    # the quiet caption on every image: where the inputs came from, and that part of them was authored
    authored = p.v("headline.audit_calls_authored_or_control")
    return (f"Inputs: public attack lists, real app names and file paths (checksum-pinned) plus {authored} calls "
            f"authored for this audit and disclosed as such. No personal data.")


# ------------------------------------------------------------------ image 1 - outcome
def image_outcome() -> tuple[Page, str]:
    p = Page(1, "01-llm-agent-security-hardening-outcome", "Outcome", "LLM agent security: unsafe tool calls, measured")
    reqs = RESULTS["requirements"]
    tiles_head = "".join(f'<span class="lab tid">{esc(r["id"])}</span>' for r in reqs)
    orig = "".join(f'<span class="tile {"bad" if r["baseline_status"] == "broken" else "good"}">{"broken" if r["baseline_status"] == "broken" else "held"}</span>' for r in reqs)
    hard = "".join(f'<span class="tile {"bad" if r["hardened_status"] == "broken" else "good"}">{"broken" if r["hardened_status"] == "broken" else "held"}</span>' for r in reqs)
    for r in reqs:
        p.used += [f"requirements.{reqs.index(r)}.baseline_status", f"requirements.{reqs.index(r)}.hardened_status"]
    css = """
.core{display:block}
.head{position:absolute;left:0;top:0;width:700px}
.head .sub{margin-top:16px}
.focal{position:absolute;left:0;top:196px;width:960px;display:flex;align-items:flex-end;gap:28px}
.focal .grp{display:flex;flex-direction:column}
.focal .before .fig{font-size:200px;color:var(--accent)}
.focal .after .fig{font-size:284px;color:var(--ink);margin-bottom:-8px}
.focal .arr{width:104px;height:28px;margin-bottom:60px}
.focal .cap{margin-top:12px;font-family:var(--sans);font-size:16px;line-height:24px;color:var(--graphite);max-width:340px}
.focal .cap b{color:var(--ink);font-weight:600}
.strip{position:absolute;left:0;top:548px;width:960px}
.strip .row{display:grid;grid-template-columns:104px repeat(9,1fr);column-gap:8px;align-items:center;margin-top:8px}
.strip .tid{text-align:left;color:var(--graphite);letter-spacing:.04em;text-transform:none}
.tile{display:flex;align-items:center;justify-content:center;height:44px;font-family:var(--mono);font-size:12px;letter-spacing:.09em;text-transform:uppercase;font-weight:600;color:var(--paper)}.tile.bad{background:var(--accent)}.tile.good{background:var(--ok)}
.strip .rowlab{font-family:var(--mono);font-size:12px;line-height:16px;letter-spacing:.09em;text-transform:uppercase;font-weight:600;color:var(--ink)}
.strip .note{margin-top:16px;font-size:16px;line-height:24px;color:var(--graphite)}
.strip .note b{color:var(--ink);font-weight:600}
.keyline{display:flex;gap:24px;margin-top:0}
"""
    body = f"""
<section class="head" data-focal="headline">
<h1>Safer tool calls<br>for LLM agents</h1>
<p class="sub">Replayed <b>{p.v("headline.audit_calls_replayed")}</b> hostile and control calls through the real code. Tripwires, canary files and an audit hook counted what got through.</p>
</section>
<section class="focal" data-focal="figure">
<div class="grp before"><div class="fig">{p.v("headline.rule_breaking_calls_baseline")}</div><div class="cap"><b>rule-breaking calls</b> in the original code</div></div>
<span class="arr"></span>
<div class="grp after"><div class="fig">{p.v("headline.rule_breaking_calls_hardened")}</div><div class="cap"><b>rule-breaking calls</b> after the fix</div></div>
</section>
<section class="strip">
<div class="row"><span></span>{tiles_head}</div>
<div class="row"><span class="rowlab">Original</span>{orig}</div>
<div class="row"><span class="rowlab">Hardened</span>{hard}</div>
<p class="note"><b>{p.v("headline.requirements_broken_baseline")} of {p.v("headline.requirements_total")}</b> safety requirements broken in the original; <b>{p.v("headline.requirements_broken_hardened")} of {p.v("headline.requirements_total")}</b> after. One tile per safety requirement.</p>
</section>
"""
    return p, page_html(p, css, body, caption_text(p))


# ------------------------------------------------------------------ image 2 - task
def image_task() -> tuple[Page, str]:
    p = Page(2, "02-ai-agent-tool-security-requirements-task", "Task", "AI agent security: nine safety rules for tool calls")
    reqs = RESULTS["requirements"]
    rows = []
    for i, r in enumerate(reqs):
        bad = r["baseline_status"] == "broken"
        p.used += [f"requirements.{i}.baseline.violations", f"requirements.{i}.baseline.cases",
                   f"requirements.{i}.hardened.violations", f"requirements.{i}.baseline_status"]
        rows.append(f"""<div class="tr{' broke' if bad else ''}">
<span class="c id">{esc(r['id'])}</span>
<span class="c rule">{esc(r['requirement'])}</span>
<span class="c map">{esc(short_risk(r['owasp_llm_2025_mapping']))}</span>
<span class="c orig {'red' if bad else 'grn'}"><i class="sq {'bad' if bad else 'good'}"></i>{n(r['baseline']['violations'])} of {n(r['baseline']['cases'])}</span>
<span class="c hard grn"><i class="sq good"></i>{n(r['hardened']['violations'])} of {n(r['hardened']['cases'])}</span>
</div>""")
    css = """
.head{position:absolute;left:0;top:0;width:560px}
.head .sub{margin-top:16px;font-size:18px;line-height:28px}
.big{position:absolute;left:600px;top:0;width:360px;height:224px}
.big .fig{position:absolute;left:0;top:-4px;font-size:192px;color:var(--accent)}
.big .of{position:absolute;left:134px;top:84px;font-family:var(--display);font-weight:800;font-size:96px;line-height:1;color:var(--ink);letter-spacing:-.02em}
.big .cap{position:absolute;left:4px;top:184px;width:340px}
.table{position:absolute;left:0;top:252px;width:960px}
.th,.tr{display:grid;grid-template-columns:56px 412px 196px 160px 136px;align-items:center;column-gap:0}
.th{height:32px;border-bottom:2px solid var(--ink)}
.th .lab{font-size:12px}
.tr{height:40px;border-bottom:1px solid var(--hair);position:relative}
.tr .c{font-size:16px;line-height:24px;white-space:nowrap}
.tr .id{font-family:var(--mono);font-weight:600;font-size:14px}
.tr .map{font-family:var(--mono);font-size:12px;letter-spacing:.02em;color:var(--graphite)}
.tr .orig,.tr .hard{font-weight:600}
.tr.broke::before{content:"";position:absolute;left:-12px;top:6px;bottom:6px;width:4px;background:var(--accent)}
.tnote{position:absolute;left:0;top:656px;width:960px;font-size:14px;line-height:20px;color:var(--graphite)}
"""
    body = f"""
<section class="head" data-focal="headline">
<h1>Nine safety rules<br>for AI agent tools</h1>
<p class="sub">Each rule is a way a hijacked model (prompt injection) could turn a tool call into damage. The audited assistant can open apps, close processes, browse and change files.</p>
</section>
<section class="big" data-focal="figure"><div class="fig red">{p.v("headline.requirements_broken_baseline")}</div><div class="of">/{p.v("headline.requirements_total")}</div><div class="lab cap">rules the original code broke</div></section>
<section class="table">
<div class="th"><span class="lab">Rule</span><span class="lab">Requirement</span><span class="lab">Risk class</span><span class="lab">Original</span><span class="lab">Hardened</span></div>
{''.join(rows)}
</section>
<p class="tnote">Counts are rule-breaking calls out of all calls replayed for that rule. Risk-class labels follow the OWASP Top 10 for LLM Applications (2025); the mapping is the author's.</p>
"""
    return p, page_html(p, css, body, caption_text(p))


# ------------------------------------------------------------------ image 3 - approach
def image_approach() -> tuple[Page, str]:
    p = Page(3, "03-llm-agent-tool-call-audit-approach", "Approach", "LLM agent audit approach: hostile input in, evidence out")
    patch = RESULTS["patch"]
    p.used += ["patch.source_files_modified", "patch.test_files_added"]
    nodes = [
        ("01", "Scripted model", "Emits the raw tool-call JSON a hijacked model could send", False),
        ("02", "Real parser and orchestrator", "The production code path, unchanged", False),
        ("03", "Dispatcher", "Routes each call to a tool", False),
        ("04", "Tool engine", "Opens apps, closes processes, browses, changes files", False),
        ("05", "Independent judges", "Tripwire programs, canary files, audit hook, real pgrep", True),
    ]
    node_html = []
    for i, (num, title, desc, dark) in enumerate(nodes):
        node_html.append(f'<div class="node{" dark" if dark else ""}"><span class="lab">{num}</span><b>{esc(title)}</b><span>{esc(desc)}</span></div>')
        if i < len(nodes) - 1:
            node_html.append('<span class="link"></span>')
    fixes = [
        ("Only the user can confirm", "The dispatcher strips any model-set confirmation; the spoken yes arrives out of band.", "registry.py · orchestrator.py"),
        ("Exact process names", "Names are validated and regex-escaped, so one call can only signal the process it names.", "system_tools.py"),
        ("Bare program names only", "No paths, options or URLs can start a program.", "system_tools.py"),
        ("Web addresses checked", "The browser gets only well-formed http and https addresses.", "system_tools.py"),
        ("Files protected", "Startup, SSH and credential files, the root folder itself and silent overwrites need protection.", "system_tools.py"),
    ]
    fx = "".join(f'<div class="fix"><span class="lab">{i + 1:02d}</span><b>{esc(t)}</b><span>{esc(d)}</span><span class="file">{esc(f)}</span></div>'
                 for i, (t, d, f) in enumerate(fixes))
    css = """
.head{position:absolute;left:0;top:0;width:520px}
.head .sub{margin-top:16px;font-size:18px;line-height:28px}
.big{position:absolute;left:556px;top:0;width:404px;height:176px}
.big .fig{font-size:108px;color:var(--ink);position:absolute;left:0;top:-4px;white-space:nowrap}
.big .fig small{font-size:48px;letter-spacing:0}
.big .cap{position:absolute;left:4px;top:120px;width:380px}
.pipe{position:absolute;left:0;top:224px;width:960px;display:flex;align-items:stretch}
.node{width:156px;min-height:156px;border:1px solid var(--ink);padding:12px 12px 12px;display:flex;flex-direction:column;gap:8px;background:var(--paper)}
.node b{font-family:var(--display);font-weight:600;font-size:19px;line-height:24px}
.node span:last-child{font-size:14px;line-height:20px;color:var(--graphite)}
.node.dark{background:var(--ink);color:var(--paper)}
.node.dark .lab,.node.dark span:last-child{color:var(--hair)}
.link{position:relative;width:45px;flex:none}
.link::before{content:"";position:absolute;left:4px;right:12px;top:50%;height:2px;margin-top:-1px;background:var(--ink)}
.link::after{content:"";position:absolute;right:4px;top:50%;margin-top:-6px;border-left:10px solid var(--ink);border-top:6px solid transparent;border-bottom:6px solid transparent}
.rule{position:absolute;left:0;top:408px;width:960px;font-size:16px;line-height:24px;color:var(--graphite);border-left:4px solid var(--accent);padding-left:16px}
.rule b{color:var(--ink);font-weight:600}
.fixes{position:absolute;left:0;top:484px;width:960px;display:grid;grid-template-columns:repeat(5,1fr);column-gap:16px}
.fix{border-top:2px solid var(--ink);padding-top:12px;display:flex;flex-direction:column;gap:8px;min-height:216px}
.fix b{font-family:var(--display);font-weight:600;font-size:19px;line-height:24px}
.fix span:nth-child(3){font-size:14px;line-height:20px;color:var(--graphite)}
.fix .file{margin-top:auto;min-height:32px;display:flex;align-items:flex-end;font-family:var(--mono);font-size:12px;line-height:16px;color:var(--ink);font-weight:500}
"""
    src_files = len(patch["source_files_modified"])
    body = f"""
<section class="head" data-focal="headline">
<h1>Hostile input in.<br>Evidence out.</h1>
<p class="sub">Only the model is scripted. Everything between its output and the tools is the real code.</p>
</section>
<section class="big" data-focal="figure"><div class="fig">+{p.v("patch.source_lines_added")}<small> / &minus;{p.v("patch.source_lines_removed")}</small></div><div class="lab cap">lines changed in {src_files} source files. The original {p.v("tests.original_suite_on_baseline.passed")} tests pass unchanged</div></section>
<section class="pipe">{''.join(node_html)}</section>
<p class="rule"><b>A violation is counted when attacker-chosen text reaches a tripwire, canary or audit hook</b>: a program started, a process signalled, a file touched outside its folder, an unconfirmed destructive action. The code's own return message is never used as evidence.</p>
<section class="fixes">{fx}</section>
"""
    return p, page_html(p, css, body, caption_text(p))


# ------------------------------------------------------------------ image 4 - data
def image_data() -> tuple[Page, str]:
    p = Page(4, "04-llm-agent-security-test-data-public-sources", "Data", "LLM agent security test data: real public inputs, pinned by checksum")
    sizes = RESULTS["data"]["corpus_sizes"]
    p.used.append("data.corpus_sizes")
    adversarial = sizes["cmd"] + sizes["traversal"] + sizes["sensitive_files"] + sizes["redirect"]
    benign_apps, benign_paths, benign_names = sizes["apps"], sizes["paths"], sizes["odd_names"]
    total = adversarial + benign_apps + benign_paths + benign_names
    commits = {k: v["commit"][:10] for k, v in RESULTS["data"]["sources"].items()}
    lic = {k: v["license"].split(" (")[0] for k, v in RESULTS["data"]["sources"].items()}
    p.used += ["data.sources", "data.retrieved_utc", "data.files"]
    files_by = {}
    for f in RESULTS["data"]["files"]:
        key = ("payloads" if "payloads" in f["path"] else "cask" if "cask" in f["path"] else
               "cpython" if "cpython" in f["path"] else "vscode" if "vscode" in f["path"] else "other")
        files_by.setdefault(key, []).append(f)
    SEG_COLOUR = {"payloads": "var(--accent)", "cask": "var(--ink)", "cpython": "var(--graphite)", "vscode": "var(--ok)"}
    rows_def = [
        ("payloads", "PayloadsAllTheThings", "Hostile strings", adversarial, "attack"),
        ("cask", "Homebrew Cask", "Real app names and homepages", benign_apps, "legit"),
        ("cpython", "CPython", "Real file paths", benign_paths, "legit"),
        ("vscode", "Visual Studio Code", "Unusual real file names", benign_names, "legit"),
    ]
    rows = []
    for key, name, role, count, kind in rows_def:
        nfiles = len([f for f in files_by.get(key, []) if f["path"].endswith((".txt", ".tsv")) and "LICENSE" not in f["path"]])
        rows.append(f"""<div class="tr">
<span class="c src"><i class="sq" style="background:{SEG_COLOUR[key]}"></i>{esc(name)}</span>
<span class="c role">{esc(role)}</span>
<span class="c lic">{esc(lic[key])}</span>
<span class="c cnt">{n(count)}</span>
<span class="c com">{esc(commits[key])}</span>
<span class="c fl">{nfiles}</span></div>""")
    widths = {"attack": adversarial, "apps": benign_apps, "paths": benign_paths, "names": benign_names}
    css = """
.head{position:absolute;left:0;top:0;width:560px}
.head .sub{margin-top:16px;font-size:18px;line-height:28px}
.big{position:absolute;left:560px;top:0;width:400px;height:200px}
.big .fig{position:absolute;left:0;top:0;font-size:112px;color:var(--ink);white-space:nowrap}
.big .cap{position:absolute;left:4px;top:116px;width:380px}
.bar{position:absolute;left:0;top:224px;width:960px}
.bar .track{display:flex;height:48px;gap:2px;margin-top:8px}
.bar .seg{height:48px;min-width:2px}
.bar .seg.attack{background:var(--accent)}.bar .seg.apps{background:var(--ink)}.bar .seg.paths{background:var(--graphite)}.bar .seg.names{background:var(--ok)}
.bar .legend{display:flex;gap:24px;margin-top:8px;font-size:14px;line-height:20px;color:var(--graphite)}
.bar .legend b{color:var(--ink);font-weight:600}
.table{position:absolute;left:0;top:356px;width:960px}
.th,.tr{display:grid;grid-template-columns:232px 272px 96px 120px 128px 112px;align-items:center}
.th{height:32px;border-bottom:2px solid var(--ink)}
.tr{height:48px;border-bottom:1px solid var(--hair)}
.tr .c{font-size:16px;line-height:24px;white-space:nowrap}
.tr .src{font-weight:600}.tr .com{font-family:var(--mono);font-size:13px}
.tr .cnt,.th .cnt{text-align:right;padding-right:24px}
.tr .cnt{font-weight:600}
.dnote{position:absolute;left:0;top:616px;width:960px;display:grid;grid-template-columns:1fr 1fr 1fr;column-gap:24px}
.dnote div{border-top:2px solid var(--ink);padding-top:8px;font-size:14px;line-height:20px;color:var(--graphite)}
.dnote b{display:block;color:var(--ink);font-family:var(--display);font-weight:600;font-size:19px;line-height:24px;margin-bottom:4px}
"""
    legend = (f'<div class="legend"><span><i class="sq bad"></i><b>{n(adversarial)}</b> hostile strings</span>'
              f'<span><i class="sq" style="background:var(--ink)"></i><b>{n(benign_apps)}</b> app names</span>'
              f'<span><i class="sq" style="background:var(--graphite)"></i><b>{n(benign_paths)}</b> file paths</span>'
              f'<span><i class="sq good"></i><b>{n(benign_names)}</b> file names (too narrow to see)</span></div>')
    track = "".join(f'<span class="seg {k}" style="flex:{v} 1 0"></span>' for k, v in widths.items())
    retrieved = RESULTS["data"]["retrieved_utc"][:10]
    n_files = len(RESULTS["data"]["files"])
    body = f"""
<section class="head" data-focal="headline">
<h1>Real inputs,<br>pinned by checksum</h1>
<p class="sub">Public attack lists to break the tool layer, and real app names, web addresses and file paths to prove normal use still works.</p>
</section>
<section class="big" data-focal="figure"><div class="fig">{n(total)}</div><div class="lab cap">public inputs from four pinned sources</div></section>
<section class="bar"><span class="lab">Share of inputs, drawn to scale from zero</span><div class="track">{track}</div>{legend}</section>
<section class="table">
<div class="th"><span class="lab">Source</span><span class="lab">Used as</span><span class="lab">Licence</span><span class="lab cnt">Inputs</span><span class="lab">Commit</span><span class="lab">Files</span></div>
{''.join(rows)}
</section>
<section class="dnote">
<div><b>Verified before every run</b>All {n_files} corpus files are re-hashed with SHA-256 and compared with the recorded manifest.</div>
<div><b>Retrieved {esc(retrieved)}</b>Each source is fixed to one full commit; the licence of every source is kept in the package.</div>
<div><b>Nothing scraped</b>No personal data, no restricted content, no invented inputs.</div>
</section>
"""
    return p, page_html(p, css, body, caption_text(p))


# ------------------------------------------------------------------ image 5 - proof
MUTANT_LABELS = {
    "M1-confirm": "Model may set the confirmation flag",
    "M2-overmatch": "Process pattern no longer escaped",
    "M3-pathexec": "Paths and options may start programs",
    "M4-browser": "Any scheme reaches the browser",
    "M5-sensitive": "Protected locations unguarded",
    "M6a-destroy-root": "Root folder may be deleted",
    "M6b-destroy-overwrite": "Rename overwrites without trash",
    "M7-inject": "Name check removed, shell still off",
    "M7b-inject-shell": "Launch goes through a shell",
    "M8-fsboundary": "Folder boundary check disabled",
    "M9-robust": "Dispatcher catch-all removed",
}


def image_proof() -> tuple[Page, str]:
    p = Page(5, "05-llm-agent-security-audit-verifiable-proof", "Proof", "LLM agent security audit: verifiable proof the checks can fail")
    mut = RESULTS["mutants"]
    p.used.append("mutants")
    ids = list(mut)
    planted = [k for k in ids if mut[k]["expected"] == "detected"]
    caught = sum(1 for k in planted if mut[k]["detected"])
    rows = []
    for k in ids:
        m = mut[k]
        eq = m["expected"] == "survive"
        label = MUTANT_LABELS.get(k, k)
        status = ("not flagged: correct" if eq else "caught")
        rows.append(f"""<div class="tr{' eq' if eq else ''}">
<span class="c id">{esc(k.split('-')[0])}</span><span class="c what">{esc(label)}</span>
<span class="c n">{n(m['violations'])} of {n(m['cases'])}</span>
<span class="c st {'gr' if eq else 'grn'}"><i class="sq {'neutral' if eq else 'good'}"></i>{status}</span></div>""")
    claims = RESULTS["claims"]
    p.used += ["claims", "all_claims_pass"]
    lines = "".join(f'<div class="ln"><span class="{"ok" if c["pass"] else "no"}">{"PASS" if c["pass"] else "FAIL"}</span> {esc(c["id"])}</div>' for c in claims)
    passed = sum(1 for c in claims if c["pass"])
    tests = RESULTS["tests"]
    p.used += ["tests.full_suite_on_hardened.passed", "tests.original_suite_on_baseline.passed"]
    st = RESULTS["strace_crosscheck"]["baseline"]
    p.used += ["strace_crosscheck.baseline.kernel_successful_execs_of_sandbox_tripwires"]
    live = RESULTS["live_pkill"]
    wild_b = next(c for c in live["baseline"]["calls"] if c["app_name"] == ".*")
    wild_h = next(c for c in live["hardened"]["calls"] if c["app_name"] == ".*")
    p.used += ["live_pkill.baseline.calls", "live_pkill.hardened.calls"]
    css = """
.head{position:absolute;left:0;top:0;width:600px}
.head .sub{margin-top:16px;font-size:18px;line-height:28px}
.big{position:absolute;left:640px;top:0;width:320px;height:180px}
.big .fig{position:absolute;left:0;top:-4px;font-size:112px;color:var(--ok);white-space:nowrap}
.big .cap{position:absolute;left:4px;top:116px;width:310px}
.table{position:absolute;left:0;top:216px;width:576px}
.th,.tr{display:grid;grid-template-columns:48px 256px 128px 144px;align-items:center}
.th{height:28px;border-bottom:2px solid var(--ink)}
.tr{height:28px;border-bottom:1px solid var(--hair)}
.tr .c{font-size:14px;line-height:20px;white-space:nowrap}
.tr .id{font-family:var(--mono);font-weight:600;font-size:12px}
.tr .n{text-align:right;padding-right:16px;font-weight:600}
.tr .st{font-weight:600;font-size:13px}
.tr.eq .what{color:var(--graphite)}
.cards{position:absolute;left:0;top:560px;width:576px;display:grid;grid-template-columns:repeat(3,1fr);column-gap:16px}
.card{border-top:2px solid var(--ink);padding-top:8px}
.card .fig{font-size:44px;line-height:48px;letter-spacing:-.01em;white-space:nowrap}
.card .cap{margin-top:8px;font-size:13px;line-height:18px;color:var(--graphite)}
.term{position:absolute;left:608px;top:216px;width:352px;height:504px;background:var(--ink);color:var(--paper);padding:16px 16px;font-family:var(--mono);font-size:12.5px;line-height:18px;overflow:hidden}
.term .ln{white-space:nowrap}.term .dim{color:var(--hair)}
.term .ok{color:var(--ok-on-ink);font-weight:600}.term .no{color:#FF9B85;font-weight:600}
.term .sum{margin-top:8px;font-weight:600}
"""
    body = f"""
<section class="head" data-focal="headline">
<h1>Built so the<br>checks can fail</h1>
<p class="sub">Each fix was re-introduced as a bug, one at a time. The audit had to catch it.</p>
</section>
<section class="big" data-focal="figure"><div class="fig">{caught}/{len(planted)}</div><div class="lab cap">planted bugs caught by the audit</div></section>
<section class="table">
<div class="th"><span class="lab">Bug</span><span class="lab">Planted flaw</span><span class="lab" style="text-align:right;padding-right:16px">Flagged</span><span class="lab">Result</span></div>
{''.join(rows)}
</section>
<section class="cards">
<div class="card"><div class="fig">{p.v("tests.full_suite_on_hardened.passed")}</div><div class="cap">tests pass on the fixed code: {p.v("tests.original_suite_on_baseline.passed")} original, unchanged, plus new ones</div></div>
<div class="card"><div class="fig">{n(st['kernel_successful_execs_of_sandbox_tripwires'])}</div><div class="cap">program launches in the original-code run: kernel trace, tripwire log and audit hook all agree</div></div>
<div class="card"><div class="fig">{len(wild_b['killed'])} <span class="gr">to</span> {len(wild_h['killed'])}</div><div class="cap">bystander processes killed by one wildcard call, real pkill, before and after</div></div>
</section>
<section class="term" aria-label="run_all.py claims">
<div class="ln dim">$ python run_all.py</div>
<div class="ln dim">&nbsp;</div>
{lines}
<div class="ln sum">{passed} of {len(claims)} claims pass</div>
</section>
"""
    return p, page_html(p, css, body, caption_text(p))


def main() -> int:
    OUT.mkdir(exist_ok=True)
    report = {"contrast": [], "images": {}}
    for fg, bg, minimum, use in CONTRAST_PAIRS:
        ratio = contrast(fg, bg)
        report["contrast"].append({"foreground": f"{fg} {PALETTE[fg]}", "background": f"{bg} {PALETTE[bg]}",
                                   "ratio": round(ratio, 2), "required": minimum, "used_for": use, "ok": ratio >= minimum})
        if ratio < minimum:
            print(f"contrast too low: {fg} on {bg} = {ratio:.2f} < {minimum}", file=sys.stderr)
            return 1
    for build in (image_outcome, image_task, image_approach, image_data, image_proof):
        page, text = build()
        (OUT / f"{page.slug}.html").write_text(text, "utf-8")
        report["images"][page.slug] = {"title": page.title, "results_fields": page.used}
        print(f"wrote portfolio/images/{page.slug}.html ({len(text) // 1024} KiB)")
    (HERE / "contrast.json").write_text(json.dumps(report["contrast"], indent=1) + "\n", "utf-8")
    (HERE / "provenance.json").write_text(json.dumps(report["images"], indent=1) + "\n", "utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
