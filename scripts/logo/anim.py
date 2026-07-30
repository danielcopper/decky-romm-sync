#!/usr/bin/env python3
"""Animates the mark: the sync ring turns while the button diamond folds into a
D-pad cross and back.

Two independent motions, both driven off one loop phase t in [0, 1):

  * the ring spins a whole turn per loop. Because the ring is two arcs 180°
    apart it looks like two cycles, which is why the turn count reads low.
  * the diamond morphs to the cross and back, holding at each end. `schedule`
    gives the four loop fractions where the holds and ramps meet, so the dwell
    at each shape is set in the same units as everything else.

Both motions are periodic in t, so frame 0 and frame `frames` are identical and
the loop closes without a seam — the last frame is never emitted twice.

Outputs, selected on the command line:

    anim.py --frames <dir>    write every frame as <dir>/f000.svg ...
    anim.py --frame <i>       print one frame's SVG
    anim.py --plot            print the morph and spin schedule as a table

`build.py` turns a frame directory into a GIF.
"""

from __future__ import annotations

import pathlib
from dataclasses import dataclass, replace

import gen


# --------------------------------------------------------------------------- #
# Config                                                                      #
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Animation:
    """Timing for one loop."""

    frames: int = 72
    fps: int = 25  # 72 @ 25 = 2.88s per loop

    # A whole turn per loop, clockwise. Negative reverses it.
    spin_turns: float = 1.0

    # Loop fractions where the morph's holds and ramps meet: the diamond holds
    # until [0], reaches the cross at [1], holds there until [2], and is back at
    # the diamond by [3].
    #
    # Wide ramps on purpose: spending more of the loop in motion and less parked at
    # either end is what makes the fold read as flowing rather than as two poses
    # with a transition between them.
    schedule: tuple[float, float, float, float] = (0.13, 0.37, 0.63, 0.87)

    # smootherstep leaves and enters the holds with zero acceleration as well as
    # zero speed, so there is no perceptible moment where the motion starts.
    easing: str = "smootherstep"  # linear | smoothstep | smootherstep


DEFAULT_ANIMATION = Animation()


# --------------------------------------------------------------------------- #
# Timing                                                                      #
# --------------------------------------------------------------------------- #
def ease(x: float, kind: str = "smoothstep") -> float:
    """Shape a 0..1 ramp. Anything outside 0..1 is clamped first."""
    x = min(1.0, max(0.0, x))
    if kind == "linear":
        return x
    if kind == "smoothstep":
        return x * x * (3.0 - 2.0 * x)
    if kind == "smootherstep":
        return x * x * x * (x * (6.0 * x - 15.0) + 10.0)
    raise ValueError(f"unknown easing: {kind!r}")


def morph_at(t: float, a: Animation = DEFAULT_ANIMATION) -> float:
    """How far the diamond has folded into the cross at loop phase t."""
    hold_a, to_b, hold_b, to_a = a.schedule
    t %= 1.0
    if t < hold_a:
        return 0.0
    if t < to_b:
        return ease((t - hold_a) / (to_b - hold_a), a.easing)
    if t < hold_b:
        return 1.0
    if t < to_a:
        return 1.0 - ease((t - hold_b) / (to_a - hold_b), a.easing)
    return 0.0


def spin_at(t: float, a: Animation = DEFAULT_ANIMATION) -> float:
    """Degrees the ring has turned at loop phase t."""
    return 360.0 * a.spin_turns * (t % 1.0)


def geometry_at(t: float, a: Animation = DEFAULT_ANIMATION, g: gen.Geometry | None = None) -> gen.Geometry:
    g = g or gen.DEFAULT_GEOMETRY
    return replace(g, arc_rot=g.arc_rot + spin_at(t, a))


# --------------------------------------------------------------------------- #
# Frames                                                                      #
# --------------------------------------------------------------------------- #
def frame(
    i: int,
    pal: gen.Palette,
    a: Animation = DEFAULT_ANIMATION,
    g: gen.Geometry | None = None,
    size: int = 512,
) -> str:
    """One frame as a standalone SVG."""
    t = (i % a.frames) / a.frames
    return gen.standalone(pal, geometry_at(t, a, g), size=size, morph=morph_at(t, a))


def write_frames(
    out: pathlib.Path,
    pal: gen.Palette,
    a: Animation = DEFAULT_ANIMATION,
    g: gen.Geometry | None = None,
    size: int = 512,
) -> list[pathlib.Path]:
    out.mkdir(parents=True, exist_ok=True)
    written = []
    for i in range(a.frames):
        p = out / f"f{i:03d}.svg"
        p.write_text(frame(i, pal, a, g, size))
        written.append(p)
    return written


def _plot(a: Animation = DEFAULT_ANIMATION) -> str:
    rows = ["frame     t   morph    spin"]
    for i in range(a.frames):
        t = i / a.frames
        rows.append(f"{i:5d} {t:5.3f}  {morph_at(t, a):5.3f}  {spin_at(t, a):6.1f}°")
    return "\n".join(rows)


if __name__ == "__main__":
    import sys

    argv = sys.argv[1:]
    name = argv[argv.index("--palette") + 1] if "--palette" in argv else gen.CHOSEN
    size = int(argv[argv.index("--size") + 1]) if "--size" in argv else 512
    pal = gen.BY_NAME[name]

    if "--plot" in argv:
        print(_plot())
    elif "--frame" in argv:
        print(frame(int(argv[argv.index("--frame") + 1]), pal, size=size))
    elif "--frames" in argv:
        d = pathlib.Path(argv[argv.index("--frames") + 1])
        got = write_frames(d, pal, size=size)
        print(f"{len(got)} frames -> {d}")
    else:
        print(__doc__)
