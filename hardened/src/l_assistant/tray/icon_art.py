"""
The one place L's icon is actually drawn. Used by two callers that need
to stay visually identical: app_tray.py's live tray icon (regenerated
every process start, so it always reflects the current tray.icon_color)
and scripts/generate_icon.py's static packaging/assets/app.ico (baked
into the .exe/installer at build time). Keeping the drawing logic here
instead of duplicated in both means changing the design -- or just the
accent color default -- can't accidentally leave the tray icon and the
taskbar/installer icon looking different from each other.

Geometry, not a font: a bold "L" built from two rectangles rather than
rendered text. Two reasons. First, legibility -- at 16x16 (the tray's
actual size on a 100% DPI display) most fonts' serifs/curves blur into
mud, while two straight bars stay crisp. Second, reproducibility -- a
build script that depends on a specific font being installed on
whichever machine runs it is a build script that silently produces a
different-looking icon (or crashes) on a machine that doesn't have that
font, which is exactly the kind of "works on my machine" fragility
worth designing around rather than discovering during a release build.
"""

from __future__ import annotations

BG_DARK = (13, 17, 23)  # near-black, matches modern dark-mode assistant apps


def _lerp_color(a: tuple, b: tuple, t: float) -> tuple:
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


def draw_icon(size: int, accent_hex: str = "#00C8FF") -> "object":
    """Returns a PIL Image (RGBA) of the icon at exactly `size` x `size`.
    Rendered at 4x supersampling then downsampled with LANCZOS for clean
    edges at every target size, from a 16px tray icon up to a 256px
    installer/taskbar icon -- a single sharp render at the target size
    directly (no naive resize of one large bitmap) is what keeps small
    sizes legible instead of muddy.
    """
    from PIL import Image, ImageDraw

    accent_hex = accent_hex.lstrip("#")
    accent = tuple(int(accent_hex[i : i + 2], 16) for i in (0, 2, 4))
    accent_deep = tuple(max(0, c - 60) for c in accent)  # darker shade, same hue family

    S = size * 4
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    radius = int(S * 0.22)
    draw.rounded_rectangle([0, 0, S - 1, S - 1], radius=radius, fill=(*BG_DARK, 255))

    margin = int(S * 0.26)
    stroke = int(S * 0.16)
    top, bottom, left = margin, S - margin, margin

    # Vertical stroke of the L, top-to-bottom gradient for a little depth.
    for y in range(top, bottom):
        t = (y - top) / max(1, (bottom - top))
        draw.line([(left, y), (left + stroke, y)], fill=(*_lerp_color(accent, accent_deep, t), 255))

    # Horizontal foot of the L.
    foot_right = S - margin
    draw.rectangle([left, bottom - stroke, foot_right, bottom], fill=(*accent_deep, 255))

    # Single "listening" pip -- a voice/mic nod without adding detail
    # that would vanish at 16px.
    dot_r = int(S * 0.075)
    dot_cx = foot_right - dot_r - int(S * 0.02)
    dot_cy = top + dot_r + int(S * 0.02)
    draw.ellipse(
        [dot_cx - dot_r, dot_cy - dot_r, dot_cx + dot_r, dot_cy + dot_r],
        fill=(255, 255, 255, 255),
    )

    return img.resize((size, size), Image.LANCZOS)
