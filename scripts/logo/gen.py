#!/usr/bin/env python3
"""Generates the plugin mark: a handheld's right-hand half, set in a disc.

Disc, body, display and every control are split along the anti-diagonal
(x + y = 200), so the facet edge runs unbroken across the whole mark, lighter
above-left and darker below-right. Emits an SVG contact sheet of the candidate
palettes on both a dark and a light ground, plus 64px and 32px sizes.

The outline and the grip groove are stored as ready-made paths next to this
file. Everything else is laid out on a millimetre grid and mapped into the
drawing through one constant, so a position can be nudged by changing a number
that still means something.
"""

import json
import pathlib

SHEET_BG = "#0e1116"
LIGHT_BG = "#e9eef3"

_HERE = pathlib.Path(__file__).parent
BODY = (_HERE / "deck_path.txt").read_text().strip()
LOWER_RIGHT = "300,-100 300,300 -100,300"

# The millimetre grid the inner geometry is laid out on. Anchored so that a
# 117mm-tall body spans 39.46..160.80 in drawing units (1.0371 units/mm) and its
# right edge, at 149mm, lands on 158.80.
MM = 1.0371
X0, Y0 = 4.27, 100.13


def mmx(v):
    return X0 + MM * v


def mmy(v):
    return Y0 - MM * v


def mm_rect(x_mm, y_mm, w_mm, h_mm, r_mm):
    """Centre-based rect in mm -> logo-space (x, y, w, h, rx)."""
    return (
        mmx(x_mm - w_mm / 2),
        mmy(y_mm + h_mm / 2),
        w_mm * MM,
        h_mm * MM,
        r_mm * MM,
    )


# Screen 173.7 x 106.7, centred; its left half runs out of the disc and clips.
# Every control is sized by its cap, not by the recess it sits in — the recess
# is about 0.3mm wider all round and reads visibly too large.
DSP_GEOM = mm_rect(0, 0, 173.7, 106.7, 3.0)
PAD_GEOM = mm_rect(105.785, 1.335, 32.5, 32.5, 4.375)
DOTS_GEOM = mm_rect(97.655, -24.09, 16.25, 6.5, 3.25)
# Thumbstick. The cap is r=9.309; the moulded recess around it is r=14.419 and
# is deliberately not drawn, since it reads as an oversized stick. The ring is
# the rim of the cap's dished top, drawn heavier than life so it survives small.
STK_GEOM = (mmx(102.83), mmy(34.50), 9.309 * MM)
STK_RING_OUT = (STK_GEOM[0], STK_GEOM[1], 6.55 * MM)
STK_RING_IN = (STK_GEOM[0], STK_GEOM[1], 5.35 * MM)
RING_T = ("#1b3549", "#0e2233")

# The groove parting the grip. Runs edge to edge, from the right-hand edge round
# to the bottom. Real width is well under a millimetre, which would be a hairline
# at icon sizes, so the stroke is drawn heavier.
SEAM = (_HERE / "seam_path.txt").read_text().strip()


# The wordmark is stored as outlines so no font has to be present to render it.
# It is loaded on demand because it depends on the project name, which the mark
# itself does not.
def _wordmark():
    return (
        (_HERE / "wordmark_path.txt").read_text().strip(),
        json.loads((_HERE / "wordmark_meta.json").read_text()),
    )


SEAM_T = ("#c6d9e8", "#a6bfd4")  # same light pair as the controls
SEAM_W = 1.0

# Menu key.
MENU_GEOM = mm_rect(115.125, 51.85, 9.52, 4.23, 2.115)

# Speaker grille: slots, not holes. Each row is given as end-cap pairs, so the
# rows read 4 / (1 long + 2) / 3 openings. Rows run shorter towards the bottom,
# following the shell's edge.
_SLOT_R = 0.847
_SLOT_ROWS = [
    (-34.03, [(90.18, 92.412), (94.647, 96.879), (99.115, 101.346), (103.582, 105.814)]),
    (-37.23, [(90.18, 94.646), (96.882, 99.113), (101.349, 103.581)]),
    (-40.44, [(90.18, 92.413), (94.648, 96.88), (99.116, 101.347)]),
]
SPEAKER_GEOMS = [
    mm_rect((x1 + x2) / 2, y, (x2 - x1) + 2 * _SLOT_R, 2 * _SLOT_R, _SLOT_R)
    for y, slots in _SLOT_ROWS
    for x1, x2 in slots
]

DISC_C = ("#aec6da", "#93b0c8")
DISC_D = ("#94b0c8", "#7a99b4")
BODY_T = ("#2d5876", "#1a3549")
DSP_DARK = ("#12222f", "#0a1621")
DSP_LIGHT = ("#d3e3ef", "#b0c8db")
PAD_T = ("#c6d9e8", "#a6bfd4")

_ALL = ("menu", "spk")

# The chosen mark.
FINAL = ("final", DISC_C, BODY_T, DSP_DARK, _ALL)

# Kept so alternatives stay one edit away rather than a re-derivation.
PALETTES = [
    FINAL,
    ("disc darker", DISC_D, BODY_T, DSP_DARK, _ALL),
    ("screen light", DISC_C, BODY_T, DSP_LIGHT, _ALL),
    ("both", DISC_D, BODY_T, DSP_LIGHT, _ALL),
]


def _rect(geom, fill=None):
    x, y, w, h, r = geom
    attrs = f'x="{x}" y="{y}" width="{w}" height="{h}" rx="{r}"'
    return f'<rect {attrs} fill="{fill}"/>' if fill else f"<rect {attrs}/>"


def _circle(geom, fill=None):
    cx, cy, r = geom
    attrs = f'cx="{cx}" cy="{cy}" r="{r}"'
    return f'<circle {attrs} fill="{fill}"/>' if fill else f"<circle {attrs}/>"


# ABXY diamond, sized by the button cap.
ABXY_GEOMS = [
    (mmx(x), mmy(y), 4.366 * MM) for x, y in ((131.71, 48.34), (122.95, 39.59), (140.42, 39.59), (131.71, 30.84))
]


def _faceted(uid, geom, tones, shape=_rect):
    """A shape carrying the same diagonal split as everything else."""
    return (
        f'<clipPath id="{uid}">{shape(geom)}</clipPath>'
        f"{shape(geom, tones[0])}"
        f'<g clip-path="url(#{uid})">'
        f'<polygon points="{LOWER_RIGHT}" fill="{tones[1]}"/>'
        f"</g>"
    )


def _seam(uid):
    """The groove, stroked, carrying the diagonal split like every filled shape."""
    stroke = f'stroke-width="{SEAM_W}" fill="none" stroke-linecap="round" stroke-linejoin="round"'
    return (
        f'<clipPath id="lr{uid}"><polygon points="{LOWER_RIGHT}"/></clipPath>'
        f'<path d="{SEAM}" stroke="{SEAM_T[0]}" {stroke}/>'
        f'<g clip-path="url(#lr{uid})">'
        f'<path d="{SEAM}" stroke="{SEAM_T[1]}" {stroke}/>'
        f"</g>"
    )


def mark(uid, pal):
    _, disc, body, dsp, extras = pal
    parts = [
        _seam(uid),
        _faceted(f"dsp{uid}", DSP_GEOM, dsp),
        _faceted(f"pad{uid}", PAD_GEOM, PAD_T),
        _faceted(f"dot{uid}", DOTS_GEOM, PAD_T),
        _faceted(f"stk{uid}", STK_GEOM, PAD_T, _circle),
        _faceted(f"rgo{uid}", STK_RING_OUT, RING_T, _circle),
        _faceted(f"rgi{uid}", STK_RING_IN, PAD_T, _circle),
    ]
    parts += [_faceted(f"ab{i}{uid}", g, PAD_T, _circle) for i, g in enumerate(ABXY_GEOMS)]
    if "menu" in extras:
        parts.append(_faceted(f"mnu{uid}", MENU_GEOM, PAD_T))
    if "spk" in extras:
        parts += [_faceted(f"sp{i}{uid}", g, PAD_T) for i, g in enumerate(SPEAKER_GEOMS)]
    inner = "".join(parts)
    return (
        f"<defs>"
        f'<clipPath id="c{uid}"><circle cx="100" cy="100" r="84"/></clipPath>'
        f'<clipPath id="b{uid}"><path d="{BODY}"/></clipPath>'
        f"</defs>"
        f'<g clip-path="url(#c{uid})">'
        f'<circle cx="100" cy="100" r="84" fill="{disc[0]}"/>'
        f'<polygon points="{LOWER_RIGHT}" fill="{disc[1]}"/>'
        f'<path d="{BODY}" fill="{body[0]}"/>'
        f'<g clip-path="url(#b{uid})">'
        f'<polygon points="{LOWER_RIGHT}" fill="{body[1]}"/>'
        f"</g>"
        f'<g clip-path="url(#b{uid})">'
        f"{inner}"
        f"</g>"
        f"</g>"
    )


def sheet():
    cols, tile, gap, pad = len(PALETTES), 200, 46, 40
    w = pad * 2 + cols * tile + (cols - 1) * gap
    h = 660
    p = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" viewBox="0 0 {w} {h}">',
        f'<rect width="{w}" height="{h}" fill="{SHEET_BG}"/>',
        f'<rect x="0" y="292" width="{w}" height="248" fill="{LIGHT_BG}"/>',
    ]
    for i, pal in enumerate(PALETTES):
        x = pad + i * (tile + gap)
        p.append(f'<g transform="translate({x},30)">{mark(f"d{i}", pal)}</g>')
        p.append(
            f'<text x="{x + tile / 2}" y="262" fill="#8fa3b8" font-size="16" '
            f'font-family="sans-serif" text-anchor="middle">{pal[0]}</text>'
        )
        p.append(f'<g transform="translate({x},316)">{mark(f"l{i}", pal)}</g>')
        for j, (sz, ox) in enumerate(((64, 30), (32, 120))):
            p.append(f'<g transform="translate({x + ox},{570}) scale({sz / 200})">{mark(f"s{i}{j}", pal)}</g>')
    p.append(
        f'<text x="{w / 2}" y="650" fill="#5a6b7d" font-size="13" '
        f'font-family="sans-serif" text-anchor="middle">'
        f"oben dunkler Grund · Mitte heller Grund · unten 64 / 32 px</text>"
    )
    p.append("</svg>")
    return "\n".join(p)


# Lockup: mark above, wordmark below. The wordmark is set a little wider than
# the disc so the pair reads as one block rather than a circle with a caption.
LOCK_PAD = 10.0
LOCK_GAP = 26.0
WM_TO_DISC = 1.35  # wordmark width relative to the disc's diameter


def lockup(pal=FINAL, ink="#1b3549", size=None):
    wordmark, _WM = _wordmark()
    disc_d = 168.0
    wm_w = disc_d * WM_TO_DISC
    s = wm_w / (_WM["bbox"][2] - _WM["bbox"][0])
    cap = -_WM["bbox"][1] * s
    w = wm_w + 2 * LOCK_PAD
    h = LOCK_PAD + disc_d + LOCK_GAP + cap + LOCK_PAD
    mark_dx = w / 2 - 100.0
    mark_dy = LOCK_PAD - 16.0  # the disc starts at y=16 inside the 200-unit mark
    base_y = LOCK_PAD + disc_d + LOCK_GAP + cap
    wm_x = LOCK_PAD - _WM["bbox"][0] * s
    width = f' width="{size}"' if size else ""
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg"{width} viewBox="0 0 {w:.1f} {h:.1f}">'
        f'<g transform="translate({mark_dx:.2f},{mark_dy:.2f})">{mark("lk", pal)}</g>'
        f'<g transform="translate({wm_x:.2f},{base_y:.2f}) scale({s:.5f})" fill="{ink}">'
        f'<path d="{wordmark}"/></g></svg>'
    )


def standalone(pal=FINAL, size=512):
    """The mark on its own, transparent outside the disc — the shipped asset."""
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{size}" height="{size}" '
        f'viewBox="0 0 200 200">{mark("m", pal)}</svg>'
    )


if __name__ == "__main__":
    import sys

    if "--lockup" in sys.argv:
        ink = sys.argv[sys.argv.index("--lockup") + 1]
        print(lockup(ink=ink))
    elif "--asset" in sys.argv:
        print(standalone())
    else:
        print(sheet())
