---
name: release-build-sourcemap
description: Release build must strip source maps in rollup.config.js, NOT in a workflow step. `decky plugin build` rebuilds the frontend itself (runs `rollup -c` in its build container) before zipping — it does not package whatever dist/ you handed it. So any workflow-level `rm -f dist/*.map` before packaging is dead weight (this is exactly how 0.18.0 shipped no asset — #763/#764). @decky/rollup hardcodes `output.sourcemap: true` and the preset's options arg is silently reverted (`mergeAndConcat(options, defaultOptions)` lets defaults win) — mutate the returned object instead. Default rollup.config.js must be the map-free one; dev opts in via rollup.dev.config.js (`pnpm build:dev` + mise build task). CI uses raw `pnpm build` → map-free.
type: project
---

# Release build — strip source maps in the rollup config, never in a workflow step

`decky plugin build` **rebuilds the frontend itself** (runs `rollup -c` in its build container) before zipping — it does
not package whatever `dist/` you handed it. So any "build then `rm -f dist/*.map` before packaging" step in
`release.yml` is dead weight: decky regenerates `dist/index.js.map` and zips it anyway. This is exactly how the 0.18.0
release shipped no asset (#763/#764): the map slipped back in and the source-map smoke-test guard failed the
`build-plugin` job.

Consequences that constrain any future change here:

- **The map must be stripped in `rollup.config.js`, not the workflow.** `@decky/rollup` hardcodes
  `output.sourcemap: true` and you **cannot** override it via the preset's options arg
  (`deckyPlugin({ output: { sourcemap: false } })` is silently reverted — `mergeAndConcat(options, defaultOptions)` lets
  the defaults win). Mutate the returned object instead:
  `const config = deckyPlugin({}); config.output.sourcemap = false;`.
- **The DEFAULT `rollup.config.js` must be the map-free one.** `decky plugin build` always runs the default `rollup -c`
  and can't be pointed at another config file — there is no CLI flag for that, and `-b` is `--build-as-root`, unrelated.
  So the map-free config is the default; dev opts _into_ maps via `rollup.dev.config.js` (inherits the default, flips
  `sourcemap` back on), wired through `pnpm build:dev` and the mise `build` task. CI uses raw `pnpm build` → map-free.
- Community plugins mostly just ship the map (e.g. MoonDeck); there is no blessed decky flag or env-var pattern for
  this. The two-config split is our own.
