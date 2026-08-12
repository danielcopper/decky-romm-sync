# Logo

The plugin mark: a button diamond ringed by a pair of sync arrows, set in a disc and split along a facet that runs
parallel to the buttons' own slant. It animates — the ring turns while the diamond folds into a d-pad cross and back.

The banner lockup sets **TENDER** beneath the mark. Versals are for the banner alone; everywhere the name appears as
text — the plugin's display name, the QAM header, toasts, prose — it is `Tender`.

![The palette candidates on a dark and a light ground](preview.png)

## Regenerating

```sh
python3 build.py --install
```

That renders everything and writes every shipped copy from the same render, so no two copies of one asset can drift
apart. Needs `rsvg-convert` and `ffmpeg` on PATH. Without `--install` it writes to `out/` instead, which is the way to
look at a change before it lands.

| File                                                      | Where it goes                                       |
| --------------------------------------------------------- | --------------------------------------------------- |
| `logo.svg`                                                | `assets/`, `docs/assets/` — MkDocs' nav-bar mark    |
| `logo.png` (512px)                                        | `assets/`, `docs/assets/` — the docs site's favicon |
| `logo-animated.gif` (512px)                               | `assets/`, `docs/assets/` — the docs landing hero   |
| `lockup.svg`, `lockup.png` (900px)                        | `assets/` — the banner at rest, light ground        |
| `lockup-dark.svg`, `lockup-dark.png`                      | `assets/` — the banner at rest, dark ground         |
| `lockup-animated.gif`, `lockup-animated-dark.gif` (600px) | `assets/` — the README banner                       |
| `store_image.png` (1024px)                                | `assets/` — the Decky store pulls this one by URL   |

Everything ships twice except the lockup, which lands once: it is the README's banner, and the docs site draws its own
header from the bare mark.

Each lockup ships in two variants because one cannot serve both grounds: the mark's ink falls to roughly 1.3:1 against
GitHub's dark canvas, so the dark variant sets the wordmark in the disc's blue instead. The README chooses between them
with `<picture>` and `prefers-color-scheme`.

The animated lockup runs the same fold and spin as the bare mark, at 600px rather than the still's 900px — a GIF pays
for every pixel in every frame, and the third multiple of the rendered width buys sharpness nobody sees at the cost of
roughly half the file again.

`gen.py` and `anim.py` also stand alone, for looking at one thing:

```sh
python3 gen.py                     # contact sheet of every palette
python3 gen.py --asset steel       # one mark, transparent outside the disc
python3 gen.py --asset --morph 1   # the D-pad end of the fold
python3 gen.py --asset --no-dots   # bare body, for judging silhouette
python3 anim.py --plot             # the morph and spin schedule, frame by frame
python3 anim.py --frame 18         # one frame's SVG
```

## Changing things

Four config objects, and nothing else worth editing:

- **`gen.Palette`** — one row per candidate in `PALETTES`; `CHOSEN` names the one that ships. Each carries two facet
  pairs, disc and ink, given as (above-left, below-right), plus the two warm dot colours. How dark a candidate can go is
  bounded at both ends — see the comment above `PALETTES`.
- **`gen.Geometry`** — every position and size, in a 200-unit square. Grouped by what they describe: the disc, the sync
  arrows, the button diamond, the dot shapes, the cross.
- **`anim.Animation`** — frame count, rate, how far the ring turns, and where the morph's holds and ramps meet.
- **`lockup`'s module constants** — which typeface was cut and at what letter-spacing, the wordmark's cap height as a
  fraction of the disc, and the air between the two. Changing the typeface or the tracking means re-cutting (below); the
  two ratios take effect on the next render.

## The wordmark

`wordmark.path` holds **TENDER** already cut into outlines, with `wordmark.json` recording what it was cut from.
Rendering therefore needs neither the typeface nor `fonttools` — only re-cutting does:

```sh
pip install fonttools brotli
python3 lockup.py --cut
```

That fetches Roboto Slab from Google Fonts (SIL Open Font License), sets the word at the configured letter-spacing,
flips it into SVG's y-down space and rewrites both files. The typeface is not vendored: it is needed by this one
function and nothing else, and a font file sitting in the repo would read as a dependency of the build, which it is not.

Roboto Slab was chosen over seven other candidates because the slab serif is the typographic register of the steam era —
railway posters, timetables, station boards — while its skeleton stays geometric enough to sit beside a mark built from
a disc, a diamond and two arrows. A true Clarendon carried the same reference but read as period costume next to it.

The letter-spacing is wide, at 0.20em. A slab's serifs already bind the letters horizontally, so it takes more tracking
than a grotesque before the word falls into separate glyphs, and the gap under the mark is set near the same value: a
wide-set wordmark tucked tight against the disc reads as inconsistent.

## Notes

Many constants carry more precision than a hand-picked number would, because they reproduce the artwork this mark comes
from. Treat those as a reference rather than as preferences: changing one is fine, it just moves away from that
reference rather than correcting an error. Where a value is deliberately off it, the field says so and the departure has
its own knob instead of overwriting the reference — `dpad_scale` and `dpad_bar_narrow` exist for that.

Two things are worth knowing before touching the drawing code:

**The facet is not 45°.** It runs parallel to the button bars, at 141.64°, because the diamond is wider than it is tall.
A facet on the plain anti-diagonal cuts across the bars instead of running with them. `facet_angle` overrides it if that
is ever wanted.

**The body is one routine, not two.** Four bars run from a hub out past a dot; at rest the two hubs sit either side of
the seam and each collinear pair merges into a capsule, and pulling both hubs to the centre turns those same four bars
into the cross. Each bar's hub end is a half-round centred exactly on the hub, which is what makes the union of two bars
smooth at any angle — move that end and the fold creases.

The GIF uses one palette for the whole sequence and no dithering. Dithering flat colour only adds grain, and costs about
a fifth of the file because LZW cannot compress noise.
