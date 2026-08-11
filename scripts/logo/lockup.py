#!/usr/bin/env python3
"""The banner lockup: the mark with TENDER set beneath it.

The wordmark ships as an outline, not as text — `wordmark.path` holds the cut
glyphs and `wordmark.json` their metrics. Rendering therefore needs neither the
typeface nor fontTools; only re-cutting does, via `--cut`.

    lockup.py                 write both variants to out/
    lockup.py --cut           re-cut the outline from the typeface, then write

Two variants ship because one cannot serve both grounds: the mark's ink
disappears on GitHub's dark canvas, so the dark variant sets the wordmark in the
disc's blue instead. The README picks between them with `<picture>` and
`prefers-color-scheme`.

Versals are for the banner alone. Everywhere the name appears as text — the
plugin's display name, the QAM header, toasts, prose — it is `Tender`.
"""

from __future__ import annotations

import json
import pathlib
import sys

import gen

HERE = pathlib.Path(__file__).parent

# What was cut, and how. Changing any of these means re-cutting: the outline is
# data, so the values below describe it rather than drive it.
FAMILY = "Roboto Slab"
WEIGHT = 700
WORD = "TENDER"
# A slab's serifs already bind the letters horizontally, so it carries more
# letter-spacing than a grotesque would before the word falls into pieces.
TRACKING_EM = 0.20

# Cap height as a fraction of the disc's diameter, and the air between the two.
# The gap is deliberately near the tracking: a wide-set wordmark tucked tight
# under the disc reads as inconsistent.
WORD_HEIGHT_RATIO = 0.20
GAP_RATIO = 0.11


def metrics() -> dict:
    return json.loads((HERE / "wordmark.json").read_text())


def outline() -> str:
    return (HERE / "wordmark.path").read_text().strip()


def geometry() -> tuple[float, float, float]:
    """Scale factor and rendered size of the wordmark, in drawing units."""
    m = metrics()
    target_h = gen.VIEW * WORD_HEIGHT_RATIO
    scale = target_h / m["height"]
    return scale, m["width"] * scale, target_h


def lockup(pal: gen.Palette, dark: bool = False, g: gen.Geometry = gen.DEFAULT_GEOMETRY) -> str:
    """The mark over the wordmark, centred on a shared vertical axis.

    On a dark ground the wordmark takes the disc's blue: the ink that carries it
    on white falls to roughly 1.3:1 against GitHub's canvas and vanishes.
    """
    scale, word_w, word_h = geometry()
    gap = gen.VIEW * GAP_RATIO
    total_w = max(gen.VIEW, word_w)
    total_h = gen.VIEW + gap + word_h
    ink = pal.disc[0] if dark else pal.ink[0]

    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {total_w:.2f} {total_h:.2f}" '
        f'width="{total_w:.0f}" height="{total_h:.0f}">'
        f'<g transform="translate({(total_w - gen.VIEW) / 2:.2f} 0)">{gen.mark("lk", pal, g)}</g>'
        f'<g transform="translate({(total_w - word_w) / 2:.2f} {gen.VIEW + gap:.2f}) '
        f'scale({scale:.6f})" fill="{ink}"><path d="{outline()}"/></g>'
        f"</svg>"
    )


def cut() -> None:
    """Re-cut the outline from the typeface. Needs `fonttools` and the network.

    The typeface is fetched rather than vendored: it is only ever needed by this
    one function, and a font file in the repo would read as a dependency of the
    build, which it is not.
    """
    # Imported here rather than at module scope: rendering must not need
    # fontTools, and every other entry point in this folder only renders.
    import io
    import re
    import urllib.request

    from fontTools.misc.transform import Transform
    from fontTools.pens.boundsPen import BoundsPen
    from fontTools.pens.recordingPen import RecordingPen
    from fontTools.pens.svgPathPen import SVGPathPen
    from fontTools.pens.transformPen import TransformPen
    from fontTools.ttLib import TTFont

    # The CSS endpoint answers with woff2 only for a browser-shaped UA.
    ua = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

    def fetch(url: str) -> bytes:
        req = urllib.request.Request(url, headers={"User-Agent": ua})
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.read()

    family = FAMILY.replace(" ", "+")
    css = fetch(f"https://fonts.googleapis.com/css2?family={family}:wght@{WEIGHT}").decode()
    urls = re.findall(r"url\((https://[^)]+\.woff2)\)", css)
    if not urls:
        sys.exit(f"no woff2 for {FAMILY} {WEIGHT}")

    font = TTFont(io.BytesIO(fetch(urls[-1])))
    upem = font["head"].unitsPerEm
    cmap = font.getBestCmap()
    glyphs = font.getGlyphSet()

    recording, x = RecordingPen(), 0.0
    for ch in WORD:
        name = cmap[ord(ch)]
        glyphs[name].draw(TransformPen(recording, Transform().translate(x, 0)))
        x += glyphs[name].width + TRACKING_EM * upem

    bounds = BoundsPen(glyphs)
    recording.replay(bounds)
    x_min, y_min, x_max, y_max = bounds.bounds

    # Font space is y-up and SVG is y-down, so the outline is flipped and moved
    # onto its own tight box in one transform.
    pen = SVGPathPen(glyphs)
    recording.replay(TransformPen(pen, Transform(1, 0, 0, -1, -x_min, y_max)))

    (HERE / "wordmark.path").write_text(pen.getCommands() + "\n")
    (HERE / "wordmark.json").write_text(
        json.dumps(
            {
                "family": FAMILY,
                "weight": WEIGHT,
                "word": WORD,
                "tracking_em": TRACKING_EM,
                "upem": upem,
                "width": x_max - x_min,
                "height": y_max - y_min,
            },
            indent=2,
        )
        + "\n"
    )
    print(f"cut {WORD} from {FAMILY} {WEIGHT} at {TRACKING_EM}em: {x_max - x_min:.0f} x {y_max - y_min:.0f} units")


if __name__ == "__main__":
    if "--cut" in sys.argv[1:]:
        cut()
    out = HERE / "out"
    out.mkdir(parents=True, exist_ok=True)
    pal = gen.BY_NAME[gen.CHOSEN]
    for name, dark in (("lockup.svg", False), ("lockup-dark.svg", True)):
        (out / name).write_text(lockup(pal, dark))
        print(f"  {name}  ({(out / name).stat().st_size:,}b)")
