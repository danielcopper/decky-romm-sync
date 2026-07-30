#!/usr/bin/env python3
"""Generates the plugin mark: a button diamond ringed by a pair of sync arrows,
set in a disc and split along a facet.

Everything is driven by two small config objects — a `Palette` (colours) and a
`Geometry` (positions and sizes) — so the mark can be re-derived and nudged
without touching the drawing code. A `Palette` carries two facet pairs: the disc
tones and the "ink" tones (arrows + button bars), each given as (above-left,
below-right) for the split. The two dot colours are the warm accents and are
meant to stay put; the blue work happens in the pairs.

The four dots sit on a diamond that is **wider than it is tall** — a stretched
one, not a square. That stretch is what tilts the bars joining adjacent dots past
45°, to 141.6°, and the facet follows the bars, so the seam and the bars share one
angle. Squaring the diamond (`cap_stagger` to 0, `dot_along` to half of `cap_sep`)
would pull both back to 45° together.

Many of the constants below carry more precision than a hand-picked number would.
They are that specific because they reproduce the artwork this mark comes from;
treat them as a reference rather than as preferences, and where one is
deliberately off that reference the field says so.

The body is drawn as four bars, each running from a hub out past one dot. At rest
the two hubs sit either side of the seam, so each pair of collinear bars merges
into one capsule. Pull the hubs to the centre (`morph` -> 1) and the same four
bars become a D-pad cross — that is the whole shape animation, and it is why
there is only one body routine rather than two. Only a bar's outer corners are
rounded, and by how much depends on the morph: a capsule wants the full
semicircle, a D-pad's arms want ends much closer to square.

Outputs, selected on the command line:

    gen.py                    an SVG contact sheet of every PALETTE (default)
    gen.py --asset [name]     one mark on its own, transparent outside the disc
    gen.py --morph <0..1>     with --asset, the mark part-way to the cross
    gen.py --no-dots          with --asset, the bare body — for judging silhouette
    gen.py --list             the palette names, one per line
"""

from __future__ import annotations

import math
from dataclasses import dataclass

VIEW = 200.0
CX = CY = VIEW / 2.0


# --------------------------------------------------------------------------- #
# Config                                                                      #
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Palette:
    """A colour set for the mark. Facet pairs are (above-left, below-right)."""

    name: str
    disc: tuple[str, str]  # the disc's two facet tones
    ink: tuple[str, str]  # arrows + button bars: light side / dark side
    dot_tan: str  # the two dots above-left of the seam (up, left)
    dot_peach: str  # the two dots below-right of the seam (down, right)
    dot_stroke: str = "none"  # optional ring around every dot
    dot_stroke_w: float = 0.0


@dataclass(frozen=True)
class Geometry:
    """Positions and sizes, all in the 200-unit drawing square."""

    disc_r: float = 100.0

    # Sync arrows: two point-symmetric arcs about the disc centre, one over the
    # top and one under the bottom, each capped with a filled arrowhead that
    # points the way the ring turns. The arrowhead is wider than the stroke it
    # caps, so `arrow_half` and `arrow_round` want to track `arc_w` when it moves.
    arc_r: float = 74.05
    arc_span: float = 140.0  # degrees swept by each arc
    arc_w: float = 15.5  # stroke width
    arc_rot: float = 6.34  # orientation offset; the animation's spin knob
    arrow_len: float = 22.5  # arrowhead reach along the tangent
    arrow_half: float = 13.8  # arrowhead half-width across the stroke
    arrow_back: float = 3.2  # degrees the head's base sits behind the stroke's end
    # The head is stroked in its own colour with a round join, which softens all
    # three corners and inflates it by half this width. Blunting the point that
    # way is what keeps the arrow from reading as a sharp dart. 0 disables it.
    arrow_round: float = 4.3

    # The button diamond. It is a *stretched* diamond, not a square one: wider
    # than it is tall, which is what tilts the bars past 45° and slides the two
    # hubs along their own axis relative to each other (`cap_stagger`). Zeroing
    # the stagger and setting `dot_along` to half of `cap_sep` would square it.
    # The capsule length is not a field: it follows as 2 * (dot_along + cap_w / 2),
    # because a bar's round cap is centred on its dot.
    cap_angle: float = 141.64  # tilt of the bar axis, degrees
    cap_w: float = 31.38  # bar width
    cap_sep: float = 40.96  # hub-to-hub across the axis
    cap_stagger: float = 9.86  # hub-to-hub along the axis; what skews the diamond
    dot_along: float = 22.27  # dot offset from its hub, along the axis
    dot_r: float = 13.63

    # Dot shape while morphing. At morph 1 each dot is a triangle pointing away
    # from the centre, with this much of its radius spent on the corner fillets:
    # 1 keeps the circle, 0 is a sharp triangle, and the default leaves the edges
    # visibly straight while the corners stay soft.
    dot_tri_blend: float = 0.42
    # The dots also draw in as the cross forms, reaching less far than the resting
    # circles' radius by this fraction. 1 would keep them one size throughout.
    dot_shrink: float = 0.862

    # How round the cross's arm ends are, as a fraction of half the bar width. 1 is
    # a semicircle, 0 a square end. This sits deliberately short of the semicircle,
    # which reads rounder than the pad wants. Only applies at full morph, so the
    # static asset keeps its capsule either way.
    dpad_end_round: float = 0.8
    # How far the cross's arms run past their dots. At rest this is half the bar
    # width, because a capsule's end cap is centred on its dot; the cross reaches
    # further, so the dots sit inside the arms rather than capping them.
    dpad_overhang: float = 22.2
    # Size of the whole cross, relative to the reference the constants above come
    # from. Below 1 the pad sits smaller in the disc without changing its
    # proportions, since dot reach and overhang scale together. It is its own field
    # so the reference stays readable next to the choice made against it.
    dpad_scale: float = 0.93
    # Bar width at full morph, as a fraction of the resting width — that width
    # belongs to the resting capsule, so only the cross gets to be leaner. Do not
    # take it below the point where a dot's reach (dot_r * dot_shrink) exceeds half
    # the narrowed bar, or the dots break out of the arms.
    dpad_bar_narrow: float = 0.9

    # The facet's direction. None keeps it parallel to the bars, which is what
    # makes the seam line up with their slant; set a number to break that
    # deliberately.
    facet_angle: float | None = None

    @property
    def facet_deg(self) -> float:
        return self.cap_angle if self.facet_angle is None else self.facet_angle

    @property
    def cap_len(self) -> float:
        """Length of one merged capsule, end cap to end cap."""
        return 2.0 * (self.dot_along + self.cap_w / 2.0)


# The stock geometry — the default everything renders at unless a caller passes
# its own (the animation frames do, to spin the arrows and pull the hubs in).
DEFAULT_GEOMETRY = Geometry()


# --------------------------------------------------------------------------- #
# Geometry helpers                                                            #
# --------------------------------------------------------------------------- #
def _pt(cx: float, cy: float, r: float, deg: float) -> tuple[float, float]:
    a = math.radians(deg)
    return cx + r * math.cos(a), cy + r * math.sin(a)


def _half_plane(g: Geometry) -> str:
    """Polygon points covering the darker side of the facet.

    The facet line runs through the disc centre along `facet_deg`; the darker
    side is the one the normal rotated -90° off that direction points into
    (right and down). Extended well past the viewBox so it clips cleanly to
    whatever shape it is applied to.
    """
    th = math.radians(g.facet_deg)
    dx, dy = math.cos(th), math.sin(th)
    nx, ny = math.sin(th), -math.cos(th)
    reach = 4.0 * VIEW
    a = (CX + dx * reach, CY + dy * reach)
    b = (CX - dx * reach, CY - dy * reach)
    c = (b[0] + nx * reach, b[1] + ny * reach)
    d = (a[0] + nx * reach, a[1] + ny * reach)
    return " ".join(f"{x:.1f},{y:.1f}" for x, y in (a, b, c, d))


def _arc_d(cx: float, cy: float, r: float, a0: float, a1: float) -> str:
    """SVG path for the arc from a0 to a1 (degrees, clockwise in screen space)."""
    x0, y0 = _pt(cx, cy, r, a0)
    x1, y1 = _pt(cx, cy, r, a1)
    large = 1 if abs(a1 - a0) > 180 else 0
    sweep = 1 if a1 > a0 else 0
    return f"M {x0:.3f} {y0:.3f} A {r:.3f} {r:.3f} 0 {large} {sweep} {x1:.3f} {y1:.3f}"


def _arrowhead(cx: float, cy: float, r: float, a_end: float, g: Geometry) -> str:
    """Filled triangle at an arc's leading end, pointing along the sweep."""
    a_base = a_end - g.arrow_back
    base = _pt(cx, cy, r, a_base)
    tan = math.radians(a_base + 90.0)  # sweep direction (increasing angle)
    rad = math.radians(a_base)  # outward-radial
    tx, ty = math.cos(tan), math.sin(tan)
    rx, ry = math.cos(rad), math.sin(rad)
    tip = (base[0] + tx * g.arrow_len, base[1] + ty * g.arrow_len)
    out = (base[0] + rx * g.arrow_half, base[1] + ry * g.arrow_half)
    inn = (base[0] - rx * g.arrow_half, base[1] - ry * g.arrow_half)
    return f"{tip[0]:.2f},{tip[1]:.2f} {out[0]:.2f},{out[1]:.2f} {inn[0]:.2f},{inn[1]:.2f}"


Corner = tuple[int, float, float, float]  # hub index, x, y, outward angle (deg)


def _diamond(g: Geometry, morph: float = 0.0) -> tuple[tuple[float, float], tuple[float, float], tuple[Corner, ...]]:
    """The two hubs and the four dots, in draw order, at a given morph.

    Hub 0 sits above-left of the seam and owns the up and left dots; hub 1 sits
    below-right and owns the down and right ones. Each dot also carries the
    direction pointing away from the disc centre, which is where its triangle
    aims while morphing.

    Morphing does three things at once. The diamond un-stretches — every dot
    slides out until all four share the widest one's radius, so the D-pad ends up
    square even though the diamond at rest is not. Each dot's bearing also swings
    onto its own axis: at rest they sit 1.4-1.9° off true, which the resting shape
    absorbs but a cross does not — left uncorrected the D-pad reads as tilted. And
    each hub slides to the centre, which is what turns a pair of collinear bars
    into one arm of a cross.
    """
    a = math.radians(g.cap_angle)
    ux, uy = math.cos(a), math.sin(a)
    vx, vy = math.cos(a + math.pi / 2.0), math.sin(a + math.pi / 2.0)
    off, stag = g.cap_sep / 2.0, g.cap_stagger / 2.0
    ul = (CX + vx * off + ux * stag, CY + vy * off + uy * stag)
    lr = (CX - vx * off - ux * stag, CY - vy * off - uy * stag)
    d = g.dot_along
    raw = [
        [0, ul[0] - ux * d, ul[1] - uy * d],
        [0, ul[0] + ux * d, ul[1] + uy * d],
        [1, lr[0] + ux * d, lr[1] + uy * d],
        [1, lr[0] - ux * d, lr[1] - uy * d],
    ]

    if morph > 0.0:
        reach = max(math.hypot(x - CX, y - CY) for _, x, y in raw) * g.dpad_scale
        for dot in raw:
            dx, dy = dot[1] - CX, dot[2] - CY
            r = math.hypot(dx, dy)
            bearing = math.degrees(math.atan2(dy, dx))
            axis = round(bearing / 90.0) * 90.0
            r += (reach - r) * morph
            th = math.radians(bearing + (axis - bearing) * morph)
            dot[1] = CX + r * math.cos(th)
            dot[2] = CY + r * math.sin(th)

    # Each hub is the midpoint of the pair it feeds, then pulled to the centre.
    hubs = []
    for which in (0, 1):
        pair = [dot for dot in raw if dot[0] == which]
        mx = (pair[0][1] + pair[1][1]) / 2.0
        my = (pair[0][2] + pair[1][2]) / 2.0
        hubs.append((mx + (CX - mx) * morph, my + (CY - my) * morph))

    corners = tuple((hub, x, y, math.degrees(math.atan2(y - CY, x - CX))) for hub, x, y in raw)
    return hubs[0], hubs[1], corners


# --------------------------------------------------------------------------- #
# Drawing                                                                     #
# --------------------------------------------------------------------------- #
def _faceted_arc(uid: str, a0: float, a1: float, ink: tuple[str, str], g: Geometry) -> str:
    """One sync arc + arrowhead, painted light then re-clipped dark below-right."""
    stroke = f'stroke-width="{g.arc_w}" fill="none" stroke-linecap="round"'
    d = _arc_d(CX, CY, g.arc_r, a0, a1)
    head = _arrowhead(CX, CY, g.arc_r, a1, g)
    # Stroking the head in its own tone is what rounds its corners; see
    # Geometry.arrow_round.
    soft = f' stroke-width="{g.arrow_round}" stroke-linejoin="round"' if g.arrow_round > 0.0 else ""
    light = (
        f'<path d="{d}" stroke="{ink[0]}" {stroke}/><polygon points="{head}" fill="{ink[0]}" stroke="{ink[0]}"{soft}/>'
    )
    dark = (
        f'<path d="{d}" stroke="{ink[1]}" {stroke}/><polygon points="{head}" fill="{ink[1]}" stroke="{ink[1]}"{soft}/>'
    )
    return (
        f"{light}"
        f'<clipPath id="lr{uid}"><polygon points="{_half_plane(g)}"/></clipPath>'
        f'<g clip-path="url(#lr{uid})">{dark}</g>'
    )


def _arm(
    hub: tuple[float, float],
    dot: tuple[float, float],
    w: float,
    end_r: float,
    overhang: float,
) -> str:
    """One bar, hub to `overhang` past its dot: half-round at the hub, `end_r` outside.

    The hub end is always a half-round centred exactly *on* the hub, never past it.
    Every bar sharing a hub therefore shares one cap circle, so the union of two
    bars at any angle is filleted by w / 2 with no crease — which is what keeps the
    silhouette smooth all the way through the fold. Square inner ends extended past
    the hub would poke their corners through that outline instead, and the crease
    shows up the moment the pair stops being collinear.

    Only the outer end takes `end_r`, so its roundness is free to differ: at
    end_r = w / 2 the two outer arcs meet and it is a semicircle.
    """
    dx, dy = dot[0] - hub[0], dot[1] - hub[1]
    span = math.hypot(dx, dy)
    if span < 1e-9:
        return ""
    ux, uy = dx / span, dy / span
    px, py = -uy, ux  # +90° in screen terms
    h = w / 2.0
    r = min(max(end_r, 0.0), h)
    bx, by = dot[0] + ux * overhang, dot[1] + uy * overhang  # outer end face
    ax, ay = hub  # the hub itself, centre of the inner half-round

    def pt(x: float, y: float) -> str:
        return f"{x:.2f} {y:.2f}"

    p0 = pt(ax + px * h, ay + py * h)
    p1 = pt(bx + px * h - ux * r, by + py * h - uy * r)
    p2 = pt(bx + px * (h - r), by + py * (h - r))
    p3 = pt(bx - px * (h - r), by - py * (h - r))
    p4 = pt(bx - px * h - ux * r, by - py * h - uy * r)
    p5 = pt(ax - px * h, ay - py * h)
    hub_cap = f"A {h:.2f} {h:.2f} 0 0 0 {p0}"
    if r < 0.01:
        square = f"{pt(bx + px * h, by + py * h)} L {pt(bx - px * h, by - py * h)}"
        return f'<path d="M {p0} L {square} L {p5} {hub_cap} Z"/>'
    arc = f"A {r:.2f} {r:.2f} 0 0 0"
    return f'<path d="M {p0} L {p1} {arc} {p2} L {p3} {arc} {p4} L {p5} {hub_cap} Z"/>'


def _body(uid: str, g: Geometry, ink: tuple[str, str], morph: float) -> str:
    """The four button bars, painted light then re-clipped dark below-right.

    At morph 0 the hubs sit apart and each collinear pair merges into a capsule;
    at morph 1 both hubs are at the centre and the bars read as a D-pad cross.
    """
    ul, lr, corners = _diamond(g, morph)
    hubs = (ul, lr)
    # The bars narrow, their ends unround and they reach further as the cross forms.
    # At morph 0 all three fall back to the resting capsule the static asset needs:
    # full width, a semicircular end, and an overhang of exactly half that width.
    rest_h = g.cap_w / 2.0
    w = g.cap_w * (1.0 + (g.dpad_bar_narrow - 1.0) * morph)
    end_r = (w / 2.0) * (1.0 + (g.dpad_end_round - 1.0) * morph)
    overhang = rest_h + (g.dpad_overhang * g.dpad_scale - rest_h) * morph
    arms = "".join(_arm(hubs[hub], (x, y), w, end_r, overhang) for hub, x, y, _ in corners)
    return (
        f'<g fill="{ink[0]}">{arms}</g>'
        f'<clipPath id="bd{uid}"><polygon points="{_half_plane(g)}"/></clipPath>'
        f'<g clip-path="url(#bd{uid})" fill="{ink[1]}">{arms}</g>'
    )


def _dot(cx: float, cy: float, g: Geometry, pal: Palette, fill: str, morph: float, out_deg: float) -> str:
    """One dot: a circle at rest, a rounded triangle pointing outward as it morphs.

    The triangle is a small triangle grown by a circular offset — three corner arcs
    joined by straight edges — not a circle whose radius is pulled in towards a
    triangle's. Blending radially bows the edges outward and reads as a swollen
    blob; a real rounded triangle keeps its edges straight and puts every bit of
    curvature in the corners. Both degenerate cases fall out: all-corner is the
    circle, no-corner the sharp triangle.
    """
    ring = ""
    if pal.dot_stroke_w > 0.0 and pal.dot_stroke != "none":
        ring = f' stroke="{pal.dot_stroke}" stroke-width="{pal.dot_stroke_w}"'
    if morph <= 0.0:
        return f'<circle cx="{cx:.3f}" cy="{cy:.3f}" r="{g.dot_r:.3f}" fill="{fill}"{ring}/>'

    # rho 1 keeps the circle, 0 sharpens to a triangle; the vertex reaches `span`
    # either way because the triangle shrinks by exactly what the offset adds.
    rho = 1.0 - morph * (1.0 - g.dot_tri_blend)
    span = g.dot_r * (1.0 + (g.dot_shrink - 1.0) * morph)
    reach = span * (1.0 - rho)  # vertex distance from the dot's centre
    fillet = span * rho  # corner radius
    sixty = math.pi / 3.0

    corners = []
    for k in range(3):
        th = math.radians(out_deg) + 2.0 * sixty * k
        vx, vy = cx + reach * math.cos(th), cy + reach * math.sin(th)
        corners.append(
            (
                (vx + fillet * math.cos(th - sixty), vy + fillet * math.sin(th - sixty)),
                (vx + fillet * math.cos(th + sixty), vy + fillet * math.sin(th + sixty)),
            )
        )

    if fillet < 0.01:
        pts = " L ".join(f"{s[0]:.2f} {s[1]:.2f}" for s, _ in corners)
        return f'<path d="M {pts} Z" fill="{fill}"{ring}/>'

    d = [f"M {corners[0][0][0]:.2f} {corners[0][0][1]:.2f}"]
    for i, (start, end) in enumerate(corners):
        if i:
            d.append(f"L {start[0]:.2f} {start[1]:.2f}")
        d.append(f"A {fillet:.2f} {fillet:.2f} 0 0 1 {end[0]:.2f} {end[1]:.2f}")
    d.append("Z")
    return f'<path d="{" ".join(d)}" fill="{fill}"{ring}/>'


def mark(
    uid: str,
    pal: Palette,
    g: Geometry = DEFAULT_GEOMETRY,
    morph: float = 0.0,
    show_dots: bool = True,
) -> str:
    """The mark's inner content — everything inside a 200-unit square, clipped to
    the disc. Wrap it in an <svg> via `standalone`, or place several on a sheet
    via `sheet`. `morph` runs 0 (button diamond) to 1 (D-pad cross); dropping the
    dots leaves the bare body, which is how its silhouette gets reviewed."""
    _, _, corners = _diamond(g, morph)
    half = g.arc_span / 2.0
    top0, top1 = 270.0 - half + g.arc_rot, 270.0 + half + g.arc_rot
    bot0, bot1 = 90.0 - half + g.arc_rot, 90.0 + half + g.arc_rot

    dots = (
        "".join(_dot(x, y, g, pal, pal.dot_tan if hub == 0 else pal.dot_peach, morph, od) for hub, x, y, od in corners)
        if show_dots
        else ""
    )
    body = (
        # disc: light fill, then the darker facet tone
        f'<circle cx="{CX}" cy="{CY}" r="{g.disc_r}" fill="{pal.disc[0]}"/>'
        f'<polygon points="{_half_plane(g)}" fill="{pal.disc[1]}"/>'
        # sync arrows
        f"{_faceted_arc(f'{uid}t', top0, top1, pal.ink, g)}"
        f"{_faceted_arc(f'{uid}b', bot0, bot1, pal.ink, g)}"
        # button bars, then the dots on top (flat; the tone reinforces the seam)
        f"{_body(uid, g, pal.ink, morph)}"
        f"{dots}"
    )
    return (
        f'<defs><clipPath id="disc{uid}"><circle cx="{CX}" cy="{CY}" r="{g.disc_r}"/></clipPath></defs>'
        f'<g clip-path="url(#disc{uid})">{body}</g>'
    )


def standalone(
    pal: Palette,
    g: Geometry = DEFAULT_GEOMETRY,
    size: int = 512,
    morph: float = 0.0,
    show_dots: bool = True,
) -> str:
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{size}" height="{size}" '
        f'viewBox="0 0 {VIEW:.0f} {VIEW:.0f}">{mark("m", pal, g, morph, show_dots)}</svg>'
    )


# --------------------------------------------------------------------------- #
# Palettes                                                                    #
# --------------------------------------------------------------------------- #
# The warm dots are shared; only the blues change between candidates.
_TAN = "#e6c7a7"
_PEACH = "#e1a38d"

PALETTES: list[Palette] = [
    # The source artwork's colours — the reference point.
    Palette("original", ("#aec6da", "#93b0c8"), ("#2d5876", "#1a3549"), _TAN, _PEACH),
    # Same disc, deeper ink so the bars and arrows sit heavier.
    Palette("deep-ink", ("#aec6da", "#93b0c8"), ("#264a63", "#12283a"), _TAN, _PEACH),
    # Disc pulled down and slightly desaturated; original ink.
    Palette("steel", ("#9db8cf", "#7f9fba"), ("#2d5876", "#1a3549"), _TAN, _PEACH),
    # Everything a notch darker and cooler.
    Palette("midnight", ("#8ba7c0", "#6f90ac"), ("#22415a", "#0e1f2e"), _TAN, _PEACH),
    # A cooler, brighter blue on a lighter disc.
    Palette("slate", ("#a6c0d6", "#88a6c0"), ("#34617f", "#1f3d52"), _TAN, _PEACH),
]

BY_NAME = {p.name: p for p in PALETTES}

# The chosen mark — what the shipped assets render from.
CHOSEN = "steel"


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
    morph = float(argv[argv.index("--morph") + 1]) if "--morph" in argv else 0.0
    if "--list" in argv:
        print("\n".join(p.name for p in PALETTES))
    elif "--asset" in argv:
        i = argv.index("--asset")
        name = argv[i + 1] if i + 1 < len(argv) and not argv[i + 1].startswith("-") else CHOSEN
        print(standalone(BY_NAME[name], morph=morph, show_dots="--no-dots" not in argv))
    else:
        print(sheet())
