#!/usr/bin/env python3
"""Generates the plugin mark: two tilted button capsules ringed by a pair of
sync arrows, set in a disc and split along a 45° facet.

Everything is driven by two small config objects — a `Palette` (colours) and a
`Geometry` (positions and sizes) — so the mark can be re-derived and nudged
without touching the drawing code. A `Palette` carries two facet pairs: the
disc tones and the "ink" tones (arrows + capsules), each given as
(above-left, below-right) for the diagonal split. The two dot colours are the
warm accents and are meant to stay put; the blue work happens in the pairs.

Outputs, selected on the command line:

    gen.py                 an SVG contact sheet of every PALETTE (default)
    gen.py --asset [name]  one mark on its own, transparent outside the disc
    gen.py --list          the palette names, one per line

The disc, the arrows and the whole inner geometry live in a 200-unit square.
The facet is a single anti-diagonal (x + y = 200): lighter above-left, darker
below-right. One polygon, clipped per shape, carries that split across the disc
and the arrows; the capsules and dots are flat, and simply take the tone of the
side they sit on so the seam reads through them too.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

VIEW = 200.0
CX = CY = VIEW / 2.0

# The half-plane below-right of the anti-diagonal x + y = VIEW, as a triangle
# large enough to cover the whole square. Clipped to a shape, it paints that
# shape's darker (below-right) side.
LOWER_RIGHT = "300,-100 300,300 -100,300"


# --------------------------------------------------------------------------- #
# Config                                                                      #
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Palette:
    """A colour set for the mark. Facet pairs are (above-left, below-right)."""

    name: str
    disc: tuple[str, str]  # the disc's two facet tones
    ink: tuple[str, str]  # arrows + capsules: light capsule / dark capsule
    dot_tan: str  # the two dots on the above-left (light-ink) capsule
    dot_peach: str  # the two dots on the below-right (dark-ink) capsule
    dot_stroke: str = "none"  # optional ring around every dot
    dot_stroke_w: float = 0.0


@dataclass(frozen=True)
class Geometry:
    """Positions and sizes, all in the 200-unit drawing square."""

    disc_r: float = 98.0

    # Sync arrows: two point-symmetric arcs about the disc centre, one over the
    # top and one under the bottom, each capped with a filled arrowhead that
    # points the way the ring turns (clockwise).
    arc_r: float = 72.0
    arc_span: float = 138.0  # degrees swept by each arc
    arc_w: float = 13.0  # stroke width
    arc_rot: float = 0.0  # orientation offset; the animation's spin knob
    arrow_len: float = 15.0  # arrowhead reach along the tangent
    arrow_half: float = 12.0  # arrowhead half-width across the stroke

    # Capsules: two parallel stadiums tilted off vertical, offset to either
    # side of the seam. Each holds two dots spaced along its long axis.
    cap_angle: float = 141.8  # tilt of the long axis, degrees
    cap_len: float = 74.0  # stadium length (rounded ends included)
    cap_w: float = 31.0  # stadium width
    cap_sep: float = 40.0  # centre-to-centre across the axis
    dot_along: float = 21.7  # dot offset from its capsule centre, along the axis
    dot_r: float = 13.3


# The stock geometry — the default everything renders at unless a caller passes
# its own (the animation frames do, to spin the arrows).
DEFAULT_GEOMETRY = Geometry()


# --------------------------------------------------------------------------- #
# Geometry helpers                                                            #
# --------------------------------------------------------------------------- #
def _pt(cx: float, cy: float, r: float, deg: float) -> tuple[float, float]:
    a = math.radians(deg)
    return cx + r * math.cos(a), cy + r * math.sin(a)


def _arc_d(cx: float, cy: float, r: float, a0: float, a1: float) -> str:
    """SVG path for the arc from a0 to a1 (degrees, clockwise in screen space)."""
    x0, y0 = _pt(cx, cy, r, a0)
    x1, y1 = _pt(cx, cy, r, a1)
    large = 1 if abs(a1 - a0) > 180 else 0
    sweep = 1 if a1 > a0 else 0
    return f"M {x0:.3f} {y0:.3f} A {r:.3f} {r:.3f} 0 {large} {sweep} {x1:.3f} {y1:.3f}"


def _arrowhead(cx: float, cy: float, r: float, a_end: float, g: Geometry) -> str:
    """Filled triangle at an arc's leading end, pointing along the sweep."""
    base = _pt(cx, cy, r, a_end)
    tan = math.radians(a_end + 90.0)  # sweep direction (increasing angle)
    rad = math.radians(a_end)  # outward-radial
    tx, ty = math.cos(tan), math.sin(tan)
    rx, ry = math.cos(rad), math.sin(rad)
    tip = (base[0] + tx * g.arrow_len, base[1] + ty * g.arrow_len)
    out = (base[0] + rx * g.arrow_half, base[1] + ry * g.arrow_half)
    inn = (base[0] - rx * g.arrow_half, base[1] - ry * g.arrow_half)
    return f"{tip[0]:.2f},{tip[1]:.2f} {out[0]:.2f},{out[1]:.2f} {inn[0]:.2f},{inn[1]:.2f}"


# --------------------------------------------------------------------------- #
# Drawing                                                                     #
# --------------------------------------------------------------------------- #
def _faceted_arc(uid: str, a0: float, a1: float, ink: tuple[str, str], g: Geometry) -> str:
    """One sync arc + arrowhead, painted light then re-clipped dark below-right."""
    stroke = f'stroke-width="{g.arc_w}" fill="none" stroke-linecap="round"'
    d = _arc_d(CX, CY, g.arc_r, a0, a1)
    head = _arrowhead(CX, CY, g.arc_r, a1, g)
    light = f'<path d="{d}" stroke="{ink[0]}" {stroke}/><polygon points="{head}" fill="{ink[0]}"/>'
    dark = f'<path d="{d}" stroke="{ink[1]}" {stroke}/><polygon points="{head}" fill="{ink[1]}"/>'
    return (
        f"{light}"
        f'<clipPath id="lr{uid}"><polygon points="{LOWER_RIGHT}"/></clipPath>'
        f'<g clip-path="url(#lr{uid})">{dark}</g>'
    )


def _capsule(cx: float, cy: float, g: Geometry, fill: str) -> str:
    x, y = cx - g.cap_len / 2.0, cy - g.cap_w / 2.0
    return (
        f'<rect x="{x:.3f}" y="{y:.3f}" width="{g.cap_len:.3f}" height="{g.cap_w:.3f}" '
        f'rx="{g.cap_w / 2.0:.3f}" fill="{fill}" '
        f'transform="rotate({g.cap_angle:.3f} {cx:.3f} {cy:.3f})"/>'
    )


def _dot(cx: float, cy: float, g: Geometry, fill: str, pal: Palette) -> str:
    ring = ""
    if pal.dot_stroke_w > 0.0 and pal.dot_stroke != "none":
        ring = f' stroke="{pal.dot_stroke}" stroke-width="{pal.dot_stroke_w}"'
    return f'<circle cx="{cx:.3f}" cy="{cy:.3f}" r="{g.dot_r:.3f}" fill="{fill}"{ring}/>'


def _capsule_group(cx: float, cy: float, g: Geometry, ink: str, dot: str, pal: Palette) -> str:
    """A capsule with its two dots spaced along the axis."""
    ux, uy = math.cos(math.radians(g.cap_angle)), math.sin(math.radians(g.cap_angle))
    d1 = _dot(cx + ux * g.dot_along, cy + uy * g.dot_along, g, dot, pal)
    d2 = _dot(cx - ux * g.dot_along, cy - uy * g.dot_along, g, dot, pal)
    return f"{_capsule(cx, cy, g, ink)}{d1}{d2}"


def mark(uid: str, pal: Palette, g: Geometry = DEFAULT_GEOMETRY) -> str:
    """The mark's inner content — everything inside a 200-unit square, clipped
    to the disc. Wrap it in an <svg> via `standalone`, or place several on a
    sheet via `sheet`."""
    perp = math.radians(g.cap_angle + 90.0)
    vx, vy = math.cos(perp), math.sin(perp)
    off = g.cap_sep / 2.0
    # +v lands above-left of the seam, -v below-right.
    ul = (CX + vx * off, CY + vy * off)
    lr = (CX - vx * off, CY - vy * off)

    half = g.arc_span / 2.0
    top0, top1 = 270.0 - half + g.arc_rot, 270.0 + half + g.arc_rot
    bot0, bot1 = 90.0 - half + g.arc_rot, 90.0 + half + g.arc_rot

    body = (
        # disc: light fill, then the below-right facet tone
        f'<circle cx="{CX}" cy="{CY}" r="{g.disc_r}" fill="{pal.disc[0]}"/>'
        f'<polygon points="{LOWER_RIGHT}" fill="{pal.disc[1]}"/>'
        # sync arrows
        f"{_faceted_arc(f'{uid}t', top0, top1, pal.ink, g)}"
        f"{_faceted_arc(f'{uid}b', bot0, bot1, pal.ink, g)}"
        # capsules + dots (flat; the tone reinforces the seam)
        f"{_capsule_group(ul[0], ul[1], g, pal.ink[0], pal.dot_tan, pal)}"
        f"{_capsule_group(lr[0], lr[1], g, pal.ink[1], pal.dot_peach, pal)}"
    )
    return (
        f'<defs><clipPath id="disc{uid}"><circle cx="{CX}" cy="{CY}" r="{g.disc_r}"/></clipPath></defs>'
        f'<g clip-path="url(#disc{uid})">{body}</g>'
    )


def standalone(pal: Palette, g: Geometry = DEFAULT_GEOMETRY, size: int = 512) -> str:
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{size}" height="{size}" '
        f'viewBox="0 0 {VIEW:.0f} {VIEW:.0f}">{mark("m", pal, g)}</svg>'
    )


# --------------------------------------------------------------------------- #
# Palettes                                                                    #
# --------------------------------------------------------------------------- #
# The warm dots are shared; only the blues change between candidates.
_TAN = "#e6c7a7"
_PEACH = "#e1a38d"

PALETTES: list[Palette] = [
    # The delivered mark, unchanged — the reference point.
    Palette("original", ("#aec6da", "#93b0c8"), ("#2d5876", "#1a3549"), _TAN, _PEACH),
    # Same disc, deeper ink so the capsules and arrows sit heavier.
    Palette("deep-ink", ("#aec6da", "#93b0c8"), ("#264a63", "#12283a"), _TAN, _PEACH),
    # Disc pulled down and slightly desaturated; original ink.
    Palette("steel", ("#9db8cf", "#7f9fba"), ("#2d5876", "#1a3549"), _TAN, _PEACH),
    # Everything a notch darker and cooler.
    Palette("midnight", ("#8ba7c0", "#6f90ac"), ("#22415a", "#0e1f2e"), _TAN, _PEACH),
    # A cooler, brighter blue on a lighter disc.
    Palette("slate", ("#a6c0d6", "#88a6c0"), ("#34617f", "#1f3d52"), _TAN, _PEACH),
]

BY_NAME = {p.name: p for p in PALETTES}


# --------------------------------------------------------------------------- #
# Contact sheet                                                               #
# --------------------------------------------------------------------------- #
SHEET_BG = "#0e1116"
LIGHT_BG = "#e9eef3"


def sheet(palettes: list[Palette] = PALETTES, g: Geometry = DEFAULT_GEOMETRY) -> str:
    cols, tile, gap, pad = len(palettes), 200, 46, 40
    w = pad * 2 + cols * tile + (cols - 1) * gap
    h = 660
    p = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" viewBox="0 0 {w} {h}">',
        f'<rect width="{w}" height="{h}" fill="{SHEET_BG}"/>',
        f'<rect x="0" y="292" width="{w}" height="248" fill="{LIGHT_BG}"/>',
    ]
    for i, pal in enumerate(palettes):
        x = pad + i * (tile + gap)
        p.append(f'<g transform="translate({x},30)">{mark(f"d{i}", pal, g)}</g>')
        p.append(
            f'<text x="{x + tile / 2}" y="262" fill="#8fa3b8" font-size="16" '
            f'font-family="sans-serif" text-anchor="middle">{pal.name}</text>'
        )
        p.append(f'<g transform="translate({x},316)">{mark(f"l{i}", pal, g)}</g>')
        for j, (sz, ox) in enumerate(((64, 30), (32, 120))):
            p.append(f'<g transform="translate({x + ox},570) scale({sz / 200})">{mark(f"s{i}{j}", pal, g)}</g>')
    p.append(
        f'<text x="{w / 2}" y="650" fill="#5a6b7d" font-size="13" '
        f'font-family="sans-serif" text-anchor="middle">'
        f"oben dunkler Grund · Mitte heller Grund · unten 64 / 32 px</text>"
    )
    p.append("</svg>")
    return "\n".join(p)


if __name__ == "__main__":
    import sys

    argv = sys.argv[1:]
    if "--list" in argv:
        print("\n".join(p.name for p in PALETTES))
    elif "--asset" in argv:
        i = argv.index("--asset")
        name = argv[i + 1] if i + 1 < len(argv) else "original"
        print(standalone(BY_NAME[name]))
    else:
        print(sheet())
