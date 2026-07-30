#!/usr/bin/env python3
"""Renders the mark's shipped files: the static asset and the animated loop.

Wraps the two external tools the drawing code deliberately knows nothing about
— `rsvg-convert` to rasterise an SVG and `ffmpeg` to pack frames into a GIF.
Both must be on PATH.

    build.py                      everything, into scripts/logo/out/
    build.py --out <dir>          somewhere else
    build.py --palette <name>     a palette other than the chosen one
    build.py --static             only the static SVG + PNGs
    build.py --gif                only the animated GIF
    build.py --size <px>          master raster size (default 512)

The GIF is quantised against a palette generated from the whole sequence, not
per frame, so the flat colours stay flat and the loop does not shimmer. The mark
has few enough colours that a small palette is lossless in practice, which is
most of why the file stays small.
"""

from __future__ import annotations

import pathlib
import shutil
import subprocess
import sys

import anim
import gen

HERE = pathlib.Path(__file__).parent
PNG_SIZES = (512, 256, 128, 64, 32)

# One palette for the whole sequence, then mapped against it. `stats_mode=full`
# looks at every frame, so a colour that only appears mid-morph still gets a slot.
#
# Dithering is off on purpose. The mark is flat colour over flat colour, so the
# only thing a dither adds is per-pixel noise — which reads as grain *and* costs
# a fifth of the file, because LZW cannot compress it. 24 slots cover the ten
# real colours plus the antialiased edges between them with no visible banding.
_GIF_FILTER = (
    "split[a][b];"
    "[a]palettegen=max_colors=24:stats_mode=full:reserve_transparent=1[p];"
    "[b][p]paletteuse=dither=none:diff_mode=rectangle"
)


def _require(*tools: str) -> None:
    missing = [t for t in tools if shutil.which(t) is None]
    if missing:
        sys.exit(f"missing on PATH: {', '.join(missing)}")


def _run(cmd: list[str]) -> None:
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        sys.exit(f"{cmd[0]} failed:\n{proc.stderr.strip()}")


def build_static(out: pathlib.Path, pal: gen.Palette, size: int) -> None:
    _require("rsvg-convert")
    out.mkdir(parents=True, exist_ok=True)
    svg = out / "logo.svg"
    svg.write_text(gen.standalone(pal, size=size))
    print(f"  {svg.name}  ({svg.stat().st_size:,}b)")
    for px in PNG_SIZES:
        png = out / (f"logo-{px}.png" if px != size else "logo.png")
        _run(["rsvg-convert", "-w", str(px), "-h", str(px), str(svg), "-o", str(png)])
        print(f"  {png.name}  ({png.stat().st_size:,}b)")

    sheet = out / "contact-sheet.svg"
    sheet.write_text(gen.sheet())
    _run(["rsvg-convert", "-w", "1400", str(sheet), "-o", str(out / "contact-sheet.png")])
    print("  contact-sheet.png")


def _gif(frames_glob: pathlib.Path, dest: pathlib.Path, fps: int, scale: int | None = None) -> None:
    vf = _GIF_FILTER if scale is None else f"scale={scale}:{scale}:flags=lanczos,{_GIF_FILTER}"
    _run(
        [
            *("ffmpeg", "-v", "error", "-y"),
            *("-framerate", str(fps)),
            *("-i", str(frames_glob)),
            *("-vf", vf),
            *("-loop", "0", str(dest)),
        ]
    )


def build_gif(out: pathlib.Path, pal: gen.Palette, size: int, a: anim.Animation) -> None:
    _require("rsvg-convert", "ffmpeg")
    out.mkdir(parents=True, exist_ok=True)
    work = out / "frames"
    if work.exists():
        shutil.rmtree(work)
    svgs = anim.write_frames(work, pal, a, size=size)
    for i, svg in enumerate(svgs):
        _run(["rsvg-convert", "-w", str(size), "-h", str(size), str(svg), "-o", str(work / f"f{i:03d}.png")])

    pattern = work / "f%03d.png"
    gif = out / "logo-animated.gif"
    _gif(pattern, gif, a.fps)
    print(f"  {gif.name}  ({gif.stat().st_size:,}b, {a.frames} frames @ {a.fps}fps)")

    # A smaller one for inline use, where 512 is far more than the slot needs.
    small = out / "logo-animated-256.gif"
    _gif(pattern, small, a.fps, scale=256)
    print(f"  {small.name}  ({small.stat().st_size:,}b)")


if __name__ == "__main__":
    argv = sys.argv[1:]
    out = pathlib.Path(argv[argv.index("--out") + 1]) if "--out" in argv else HERE / "out"
    name = argv[argv.index("--palette") + 1] if "--palette" in argv else gen.CHOSEN
    size = int(argv[argv.index("--size") + 1]) if "--size" in argv else 512
    pal = gen.BY_NAME[name]
    only_static, only_gif = "--static" in argv, "--gif" in argv

    print(f"palette: {pal.name}   out: {out}")
    if not only_gif:
        build_static(out, pal, size)
    if not only_static:
        build_gif(out, pal, size, anim.DEFAULT_ANIMATION)
