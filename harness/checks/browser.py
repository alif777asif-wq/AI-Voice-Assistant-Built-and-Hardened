"""R6 - the browser is only ever handed well-formed http(s) URLs."""
from __future__ import annotations

import os
import re
from urllib.parse import urlsplit

from harness.runtime import ClassResult, Context

KEY, RULE = "browser", "R6"
TITLE = "Browser hand-off accepts non-http schemes and option-like strings"

AUTHORED = [
    "file:///home/user/.ssh/id_rsa", "file://localhost/etc/passwd.x", "javascript://example.com/%0aalert(1)",
    "data://text/html;base64,PHNjcmlwdD4=.x", "ftp://example.com/payload.exe", "smb://attacker.example/share/x.exe",
    "ssh://user@host.example", "gopher://example.com/x", "search-ms://query=x.y",
    "--gpu-launcher=sh://x.y", "--renderer-cmd-prefix=calc://x.y", "--utility-cmd-prefix=x://y.z",
    "-new-window://x.y", "--incognito://x.y", "https://a.example\n--incognito", "http://example.com\t--x",
    "https://example.com/\x07", "https://example.com/\x00evil",
]

CTRL = {chr(i) for i in range(0, 32)} | {chr(127)}


def url_problems(url: str) -> list[str]:
    problems = []
    if url.startswith("-"):
        problems.append("begins with '-' (parsed as a browser option)")
    m = re.match(r"^\s*([A-Za-z][A-Za-z0-9+.-]*):", url)
    scheme = m.group(1).lower() if m else ""
    if scheme not in ("http", "https"):
        problems.append(f"scheme {scheme or '(none)'!r} is not http/https")
    if any(c in CTRL for c in url):
        problems.append("contains control characters")
    return problems


def judge(out) -> tuple[bool, str]:
    urls = [e["url"] for e in out.events if e["k"] == "url"]
    for t in out.tripwires:
        if os.path.basename(t[0]) == "lbrowser":
            urls += t[1:]
    problems = []
    for u in dict.fromkeys(urls):
        for p in url_problems(u):
            problems.append(f"{u[:70]!r}: {p}")
    return bool(problems), "; ".join(problems)


def handed_urls(out) -> list[str]:
    return [t[1] for t in out.tripwires if os.path.basename(t[0]) == "lbrowser" and len(t) > 1]


def run(ctx: Context) -> dict:
    res = ClassResult(KEY, RULE, TITLE)
    disp = ctx.dispatcher(ctx.engine())
    groups = [("authored-scheme-and-option", AUTHORED), ("public-open-redirect", ctx.corpus["redirect"])]
    for group, payloads in groups:
        for i, payload in enumerate(payloads):
            out = ctx.call(disp, "open_website", {"target": payload})
            bad, why = judge(out)
            res.add(f"open_website/{group}", f"{KEY}-open_website-{group}-{i}", "open_website",
                    {"target": payload}, bad, why)
    # negative control: search_web only ever builds an escaped Google/YouTube URL
    for i, payload in enumerate(ctx.corpus["redirect"] + AUTHORED):
        out = ctx.call(disp, "search_web", {"query": payload})
        bad, why = judge(out)
        res.add("search_web/negative-control", f"{KEY}-search_web-{i}", "search_web", {"query": payload}, bad, why)
    # benign: every real homepage, once as the full URL and once as a bare host name
    ok = n = 0
    for token, homepage in ([] if ctx.skip_benign else ctx.corpus["apps"]):
        host = urlsplit(homepage).hostname
        for target, expected in ((homepage, homepage), (host, f"https://{host}")):
            if not target:
                continue
            n += 1
            out = ctx.call(disp, "open_website", {"target": target})
            if out.kind == "result" and handed_urls(out) == [expected]:
                ok += 1
    res.extra["benign"] = None if ctx.skip_benign else {"cases": n, "ok": ok, "legit_failed": n - ok,
                           "source": "cask_apps.tsv homepages (full URL and bare host)"}
    return res.finish()
