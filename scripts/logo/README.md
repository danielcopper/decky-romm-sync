# Logo

`gen.py` draws the plugin mark — a handheld's right-hand half set in a disc, split along a 45° facet that runs unbroken
across disc, body, screen and every control.

## Rendering

```sh
python3 gen.py > sheet.svg
rsvg-convert -w 1300 sheet.svg -o preview.png
```

The sheet shows the candidate palettes side by side, each on a dark ground (plugin store, quick-access menu) and a light
one (docs site), plus 64px and 32px so small-size legibility stays visible while tweaking.

## Changing things

- **Palettes** — `PALETTES`, one row per candidate: disc pair, body pair, screen pair, and which optional details to
  include. Every pair is (above-left, below-right) for the facet.
- **Positions and sizes** — the block of `*_GEOM` constants. These are on a millimetre grid mapped into drawing units by
  `MM`, `X0` and `Y0`, so moving something means changing a measurement rather than guessing at coordinates.
- **Outline and groove** — `deck_path.txt` and `seam_path.txt`, plain SVG path data. Regenerate them only if the shape
  itself changes; day-to-day work does not touch them.

## Notes

Two things are deliberately drawn heavier than their real proportions, because at icon sizes a faithful version
disappears: the rim inside the thumbstick, and the groove parting the grip (`SEAM_W`).

Controls are sized by their caps, not by the recesses they sit in. The recess is noticeably wider and makes the mark
look clumsy — the thumbstick is the clearest case, where the difference is a third of the diameter.
