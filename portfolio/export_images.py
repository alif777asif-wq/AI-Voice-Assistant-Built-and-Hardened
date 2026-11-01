#!/usr/bin/env python3
"""Exports the five image HTML files to 4000x3000 px (300 DPI, sRGB) and measures every output.

    python portfolio/export_images.py

Needs: playwright with Chromium, Pillow.  Writes
  portfolio/exports/<name>.png          lossless master, 4000x3000, 300 DPI tag, sRGB profile embedded
  portfolio/exports/<name>.jpg          quality 100, 4:4:4 chroma, same tags
  portfolio/exports/<name>-upload.jpg   only if the .jpg is larger than 5 MB: highest quality that fits
  portfolio/thumbnails/<name>.png       300 px wide render used for the legibility check
  portfolio/measurements.json           dimensions, DPI, profile, chroma, sizes, safe zones, fonts, thumbnails
"""
from __future__ import annotations

import io
import json
import sys
import time
from pathlib import Path

from PIL import Image, ImageChops, ImageCms, JpegImagePlugin
from playwright.sync_api import sync_playwright

HERE = Path(__file__).resolve().parent
IMAGES = HERE / "images"
EXPORTS = HERE / "exports"
THUMBS = HERE / "thumbnails"
W, H, DSF = 1280, 960, 3.125            # 1280 x 3.125 = 4000, 960 x 3.125 = 3000
LIMIT_BYTES = 5 * 1000 * 1000           # a conservative reading of "5 MB"
SAFE_PAD = 70                           # essentials stay inside this padding on the 1280x960 artboard
CENTRAL = (160, 120, 1120, 840)         # the central 960x720 region: left, top, right, bottom

PROBE = """
() => {
  const art = document.querySelector('.art').getBoundingClientRect();
  const boxes = [];
  const skip = new Set(['HTML','BODY','HEAD','STYLE','TITLE','META','SCRIPT']);
  for (const el of document.querySelectorAll('.art *')) {
    if (skip.has(el.tagName)) continue;
    const cs = getComputedStyle(el);
    if (cs.display === 'none' || cs.visibility === 'hidden') continue;
    // text: measure the glyph boxes, not the (possibly wider) element box
    let own = false;
    for (const node of el.childNodes) if (node.nodeType === 3 && node.textContent.trim()) own = true;
    if (own) {
      const range = document.createRange(); range.selectNodeContents(el);
      for (const r of range.getClientRects()) if (r.width > 0 && r.height > 0) boxes.push({k:'text', l:r.left, t:r.top, r:r.right, b:r.bottom, tag: el.tagName});
    }
    const bg = cs.backgroundColor, border = parseFloat(cs.borderTopWidth) + parseFloat(cs.borderLeftWidth);
    const painted = (bg && bg !== 'rgba(0, 0, 0, 0)') || border > 0 || el.tagName === 'I';
    if (painted && !el.matches('.art, .core, .top, .bottom, .rail')) {
      const r = el.getBoundingClientRect();
      if (r.width > 0 && r.height > 0) boxes.push({k:'box', l:r.left, t:r.top, r:r.right, b:r.bottom, tag: el.tagName, cls: el.className});
    }
  }
  const union = (list) => list.length ? ({l:Math.min(...list.map(b=>b.l)), t:Math.min(...list.map(b=>b.t)), r:Math.max(...list.map(b=>b.r)), b:Math.max(...list.map(b=>b.b))}) : null;
  const focal = {};
  for (const el of document.querySelectorAll('[data-focal]')) {
    const list = [];
    const walker = el.querySelectorAll('*');
    const consider = [el, ...walker];
    for (const e of consider) {
      let own = false;
      for (const node of e.childNodes) if (node.nodeType === 3 && node.textContent.trim()) own = true;
      if (own) { const range = document.createRange(); range.selectNodeContents(e); for (const r of range.getClientRects()) if (r.width>0&&r.height>0) list.push({l:r.left,t:r.top,r:r.right,b:r.bottom}); }
    }
    focal[el.dataset.focal] = union(list);
  }
  const fonts = [];
  document.fonts.forEach(f => fonts.push({family: f.family.replace(/"/g,''), weight: f.weight, status: f.status}));
  const used = new Set();
  const usedFaces = new Set();
  for (const el of document.querySelectorAll('.art *')) {
    const cs = getComputedStyle(el);
    used.add(cs.fontFamily.split(',')[0].replace(/["']/g,'').trim());
    if ([...el.childNodes].some(n => n.nodeType === 3 && n.textContent.trim()))
      usedFaces.add(cs.fontFamily.split(',')[0].replace(/["']/g,'').trim() + '|' + cs.fontWeight);
  }
  const smallest = Math.min(...Array.from(document.querySelectorAll('.art *')).filter(e => [...e.childNodes].some(n => n.nodeType===3 && n.textContent.trim())).map(e => parseFloat(getComputedStyle(e).fontSize)));
  const h1 = document.querySelector('h1').getBoundingClientRect();
  const fig = document.querySelector('[data-focal="figure"] .fig');
  const figSize = fig ? parseFloat(getComputedStyle(fig).fontSize) : null;
  return {usedFaces: [...usedFaces], art:{w:art.width,h:art.height}, everything: union(boxes), text: union(boxes.filter(b=>b.k==='text')), focal, fonts, usedFamilies:[...used], smallestFontPx: smallest,
          h1FontPx: parseFloat(getComputedStyle(document.querySelector('h1')).fontSize), figureFontPx: figSize,
          checks: {display: document.fonts.check('800 52px Fraunces'), sans: document.fonts.check('500 16px "IBM Plex Sans"'), mono: document.fonts.check('500 12px "IBM Plex Mono"')},
          overflowX: document.documentElement.scrollWidth, overflowY: document.documentElement.scrollHeight};
}
"""


PAPER = (0xF2, 0xEF, 0xE7)


def ink_box(browser, html: Path, key: str, glyph_box: dict) -> dict:
    """Bounding box (CSS px) of the pixels a focal element really paints: all other elements are hidden,
    the element is rendered alone on the paper, and every pixel that differs from the paper is counted."""
    ctx = browser.new_context(viewport={"width": W, "height": H}, device_scale_factor=2)
    page = ctx.new_page()
    page.goto(html.resolve().as_uri())
    page.evaluate("document.fonts.ready")
    page.add_style_tag(content=".art *{visibility:hidden !important}")
    page.evaluate("""(k) => { for (const e of document.querySelectorAll('[data-focal="' + k + '"], [data-focal="' + k + '"] *'))
                    e.style.setProperty('visibility', 'visible', 'important'); }""", key)
    raw = page.screenshot(type="png", full_page=False)
    ctx.close()
    with Image.open(io.BytesIO(raw)) as im:
        im = im.convert("RGB")
        scale = im.size[0] / W
        channels = [Image.eval(c, lambda v, t=t: 255 if abs(v - t) > 10 else 0) for c, t in zip(im.split(), PAPER)]
        mask = ImageChops.lighter(ImageChops.lighter(channels[0], channels[1]), channels[2])
        bbox = mask.getbbox()
    if bbox is None:
        raise RuntimeError(f"no ink found for focal element {key}")
    l, t, rr, bb = (v / scale for v in bbox)
    return {"l": l, "t": t, "r": rr, "b": bb}


def srgb_profile() -> tuple[bytes, str]:
    profile = ImageCms.createProfile("sRGB")
    data = ImageCms.ImageCmsProfile(profile).tobytes()
    desc = ImageCms.getProfileDescription(ImageCms.ImageCmsProfile(io.BytesIO(data))).strip()
    return data, desc


def inside(box: dict, region: tuple) -> bool:
    l, t, r, b = region
    return box["l"] >= l - 0.01 and box["t"] >= t - 0.01 and box["r"] <= r + 0.01 and box["b"] <= b + 0.01


def margins(box: dict) -> dict:
    return {"left": round(box["l"], 1), "top": round(box["t"], 1), "right": round(W - box["r"], 1), "bottom": round(H - box["b"], 1)}


def measure_file(path: Path) -> dict:
    with Image.open(path) as im:
        info = {"file": str(path.relative_to(HERE.parent)), "format": im.format, "size_px": list(im.size), "mode": im.mode,
                "bytes": path.stat().st_size, "mb": round(path.stat().st_size / 1_000_000, 2)}
        dpi = im.info.get("dpi")
        info["dpi"] = [round(float(dpi[0])), round(float(dpi[1]))] if dpi else None
        icc = im.info.get("icc_profile")
        info["icc_profile"] = (ImageCms.getProfileDescription(ImageCms.ImageCmsProfile(io.BytesIO(icc))).strip() if icc else None)
        if im.format == "JPEG":
            sampling = JpegImagePlugin.get_sampling(im)
            info["chroma_subsampling"] = {0: "4:4:4", 1: "4:2:2", 2: "4:2:0"}.get(sampling, f"unknown({sampling})")
    return info


def main() -> int:
    EXPORTS.mkdir(exist_ok=True)
    THUMBS.mkdir(exist_ok=True)
    icc, icc_name = srgb_profile()
    htmls = sorted(IMAGES.glob("*.html"))
    if len(htmls) != 5:
        print(f"expected five image files, found {len(htmls)}", file=sys.stderr)
        return 1
    import PIL
    from importlib.metadata import version as pkg_version
    report = {"exported_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "viewport": [W, H], "device_scale_factor": DSF,
              "tools": {"python": sys.version.split()[0], "pillow": PIL.__version__, "playwright": pkg_version("playwright")},
              "icc_profile_embedded": icc_name, "images": {}}
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        report["chromium"] = browser.version
        for html in htmls:
            name = html.stem
            ctx = browser.new_context(viewport={"width": W, "height": H}, device_scale_factor=DSF)
            page = ctx.new_page()
            page.goto(html.resolve().as_uri())
            page.evaluate("document.fonts.ready")
            probe = page.evaluate(PROBE)
            raw = page.screenshot(type="png", full_page=False)
            ctx.close()
            with Image.open(io.BytesIO(raw)) as im:
                im = im.convert("RGB")
                assert im.size == (4000, 3000), im.size
                png = EXPORTS / f"{name}.png"
                im.save(png, format="PNG", dpi=(300, 300), icc_profile=icc, optimize=True)
                jpg = EXPORTS / f"{name}.jpg"
                im.save(jpg, format="JPEG", quality=100, subsampling=0, dpi=(300, 300), icc_profile=icc, optimize=True)
                files = [measure_file(png), measure_file(jpg)]
                if jpg.stat().st_size > LIMIT_BYTES:
                    lo, hi, best = 60, 100, None
                    while lo <= hi:
                        mid = (lo + hi) // 2
                        buf = io.BytesIO()
                        im.save(buf, format="JPEG", quality=mid, subsampling=0, dpi=(300, 300), icc_profile=icc, optimize=True)
                        if buf.tell() <= LIMIT_BYTES:
                            best, lo = (mid, buf.getvalue()), mid + 1
                        else:
                            hi = mid - 1
                    q, data = best
                    up = EXPORTS / f"{name}-upload.jpg"
                    up.write_bytes(data)
                    m = measure_file(up)
                    m["jpeg_quality"] = q
                    files.append(m)
            # thumbnail legibility render: ~300 px wide
            ctx = browser.new_context(viewport={"width": W, "height": H}, device_scale_factor=300 / W)
            page = ctx.new_page()
            page.goto(html.resolve().as_uri())
            page.evaluate("document.fonts.ready")
            page.screenshot(path=str(THUMBS / f"{name}.png"), type="png")
            ctx.close()
            ev, text, focal = probe["everything"], probe["text"], probe["focal"]
            ink = {k: ink_box(browser, html, k, v) for k, v in focal.items()}
            faces = probe["fonts"]
            used_ok = all(any(f["family"] == fam and f["weight"] == wt and f["status"] == "loaded" for f in faces)
                          for fam, wt in (x.split("|") for x in probe["usedFaces"]))
            report["images"][name] = {
                "files": files,
                "artboard_css_px": [round(probe["art"]["w"]), round(probe["art"]["h"])],
                "aspect_ratio": "4:3" if 4000 * 3 == 3000 * 4 else "?",
                "safe_zone_70px": {"everything_inside": inside(ev, (SAFE_PAD, SAFE_PAD, W - SAFE_PAD, H - SAFE_PAD)),
                                   "text_inside": inside(text, (SAFE_PAD, SAFE_PAD, W - SAFE_PAD, H - SAFE_PAD)),
                                   "margins_px_of_everything": margins(ev), "margins_px_of_text": margins(text)},
                "central_960x720": {k: {"inside": inside(ink[k], CENTRAL),
                                        "painted_pixels_box_css_px": [round(ink[k]["l"], 1), round(ink[k]["t"], 1), round(ink[k]["r"], 1), round(ink[k]["b"], 1)],
                                        "line_box_css_px": [round(v["l"], 1), round(v["t"], 1), round(v["r"], 1), round(v["b"], 1)]}
                                    for k, v in focal.items()},
                "fonts": {"faces": probe["fonts"], "used_faces": probe["usedFaces"], "all_loaded": used_ok,
                          "families_used": probe["usedFamilies"], "checks": probe["checks"]},
                "no_page_overflow": probe["overflowX"] <= W and probe["overflowY"] <= H,
                "type": {"smallest_text_px": probe["smallestFontPx"], "headline_px": probe["h1FontPx"], "figure_px": probe["figureFontPx"],
                         "figure_px_at_300w_thumbnail": round(probe["figureFontPx"] * 300 / W, 1) if probe["figureFontPx"] else None,
                         "headline_px_at_300w_thumbnail": round(probe["h1FontPx"] * 300 / W, 1)},
                "thumbnail": f"portfolio/thumbnails/{name}.png",
            }
            print(f"exported {name}: " + ", ".join(f"{Path(f['file']).name} {f['mb']} MB" for f in files))
        browser.close()
    (HERE / "measurements.json").write_text(json.dumps(report, indent=1) + "\n", "utf-8")
    bad = [k for k, v in report["images"].items()
           if not (v["safe_zone_70px"]["everything_inside"] and all(c["inside"] for c in v["central_960x720"].values())
                   and v["fonts"]["all_loaded"] and v["no_page_overflow"])]
    if bad:
        print("geometry or font check failed for: " + ", ".join(bad), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
