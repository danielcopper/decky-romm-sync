#!/usr/bin/env python3
"""Renders the mark's shipped files: the static asset, the banner lockup and the animated loop.

Wraps the two external tools the drawing code deliberately knows nothing about
— `rsvg-convert` to rasterise an SVG and `ffmpeg` to pack frames into a GIF.
Both must be on PATH.

    build.py                      everything, into scripts/logo/out/
    build.py --install            everything, into the paths the repo ships from
    build.py --out <dir>          somewhere else
    build.py --palette <name>     a palette other than the chosen one
    build.py --static             only the static SVG + PNGs and the lockup
    build.py --gif                only the animated GIF
    build.py --size <px>          master raster size (default 512)

`--install` is the one to run after changing the mark: it writes every shipped
copy from the same render, so `assets/` and `docs/assets/` cannot drift apart.

The GIF is quantised against a palette generated from the whole sequence rather
than one per frame, so the flat colours stay flat and the loop does not shimmer.
That single shared palette, and no dithering, is most of why the file stays well
under a per-frame-palette encode of the same footage.
"""

from __future__ import annotations

import pathlib
import shutil
import subprocess
import sys

import anim
import gen
import lockup

HERE = pathlib.Path(__file__).parent
PNG_SIZES = (1024, 512, 256, 128, 64, 32)
# The README renders the banner around 300px wide; three times that covers the
# densest display anyone reads it on.
LOCKUP_PNG_WIDTH = 900
# The animated banner stops at twice the rendered width instead of three times:
# a GIF pays for every pixel in every frame, and the third multiple buys sharpness
# nobody sees at the cost of roughly half the file again.
LOCKUP_GIF_WIDTH = 600


# One palette for the whole sequence, then mapped against it. `stats_mode=full`
# looks at every frame, so a colour that only appears mid-morph still gets a slot.
#
# Dithering is off on purpose. The mark is flat colour over flat colour, so the
# only thing a dither adds is per-pixel noise — which reads as grain *and* costs a
# fifth of the file, because LZW cannot compress it.
#
# The mark itself only has about ten colours; the rest of the palette goes on the
# antialiased edges between them, and at 512px there are enough edge pixels that a
# small palette starts to show. Against the rendered frames, 24 slots leave a
# worst-case channel error of 49 on 1.2% of pixels and 64 halve that to 26 on 0.3%;
# past 64 the size grows faster than the fidelity. Hence 64.
def _gif_filter(colors: int) -> str:
    return (
        "split[a][b];"
        f"[a]palettegen=max_colors={colors}:stats_mode=full:reserve_transparent=1[p];"
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


def build_lockup(out: pathlib.Path, pal: gen.Palette) -> None:
    """The banner: mark over wordmark, in the two variants the grounds need."""
    _require("rsvg-convert")
    out.mkdir(parents=True, exist_ok=True)
    for stem, dark in (("lockup", False), ("lockup-dark", True)):
        svg = out / f"{stem}.svg"
        svg.write_text(lockup.lockup(pal, dark))
        png = out / f"{stem}.png"
        _run(["rsvg-convert", "-w", str(LOCKUP_PNG_WIDTH), str(svg), "-o", str(png)])
        print(f"  {svg.name}  ({svg.stat().st_size:,}b)")
        print(f"  {png.name}  ({png.stat().st_size:,}b)")


def build_lockup_gif(out: pathlib.Path, pal: gen.Palette, a: anim.Animation) -> None:
    """The banner, animated — the same fold and spin the bare mark runs."""
    _require("rsvg-convert", "ffmpeg")
    out.mkdir(parents=True, exist_ok=True)
    for stem, dark in (("lockup-animated", False), ("lockup-animated-dark", True)):
        work = out / f"frames-{stem}"
        if work.exists():
            shutil.rmtree(work)
        svgs = lockup.write_frames(work, pal, a, dark, LOCKUP_GIF_WIDTH)
        for i, svg in enumerate(svgs):
            _run(["rsvg-convert", "-w", str(LOCKUP_GIF_WIDTH), str(svg), "-o", str(work / f"f{i:03d}.png")])
        gif = out / f"{stem}.gif"
        _gif(work / "f%03d.png", gif, a.fps)
        print(f"  {gif.name}  ({gif.stat().st_size:,}b, {LOCKUP_GIF_WIDTH}px wide)")


def _gif(
    frames_glob: pathlib.Path,
    dest: pathlib.Path,
    fps: int,
    scale: int | None = None,
    colors: int = 64,
) -> None:
    filt = _gif_filter(colors)
    vf = filt if scale is None else f"scale={scale}:{scale}:flags=lanczos,{filt}"
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
    print(f"  {gif.name}  ({gif.stat().st_size:,}b, {size}px, {a.frames} frames @ {a.fps}fps)  <- shipped")

    # A half-size one for slots that never render bigger than this. Quarter the
    # pixels means quarter the antialiased edge, so it needs far less palette to
    # stay clean — 24 measures as well here as 64 does at full size.
    small = out / "logo-animated-256.gif"
    _gif(pattern, small, a.fps, scale=256, colors=24)
    print(f"  {small.name}  ({small.stat().st_size:,}b, 256px)")


REPO = HERE.parent.parent

# Where each shipped file goes. The mark lands twice because MkDocs only serves
# what lives under docs/, and the README needs a path that resolves on GitHub.
INSTALL = {
    "logo.svg": ("assets/logo.svg", "docs/assets/logo.svg"),
    "logo.png": ("assets/logo.png", "docs/assets/logo.png"),
    "logo-animated.gif": ("assets/logo-animated.gif", "docs/assets/logo-animated.gif"),
    # The lockup lands once: it is the README's banner, and the docs site draws
    # its own header from the bare mark instead.
    "lockup.svg": ("assets/lockup.svg",),
    "lockup.png": ("assets/lockup.png",),
    "lockup-dark.svg": ("assets/lockup-dark.svg",),
    "lockup-dark.png": ("assets/lockup-dark.png",),
    "lockup-animated.gif": ("assets/lockup-animated.gif",),
    "lockup-animated-dark.gif": ("assets/lockup-animated-dark.gif",),
}
# The Decky store pulls this one straight off the default branch by URL.
STORE_IMAGE = ("logo-1024.png", "assets/store_image.png")


def install(out: pathlib.Path) -> None:
    """Copy the freshly built files over the ones the repo ships."""
    pairs = [(out / src, REPO / dest) for src, dests in INSTALL.items() for dest in dests]
    pairs.append((out / STORE_IMAGE[0], REPO / STORE_IMAGE[1]))
    for src, dest in pairs:
        if not src.exists():
            sys.exit(f"not built: {src}")
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dest)
        print(f"  {dest.relative_to(REPO)}  ({dest.stat().st_size:,}b)")


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
        build_lockup(out, pal)
    if not only_static:
        build_gif(out, pal, size, anim.DEFAULT_ANIMATION)
        build_lockup_gif(out, pal, anim.DEFAULT_ANIMATION)
    if "--install" in argv:
        print("installing:")
        install(out)
