# Dependency management

One page for the question "**why is _that_ on _this_ version, and who bumps it?**" Every version in the repo lives in
one of the files below. Most update automatically (Renovate); a few are pinned by hand on purpose.

## Where every version lives

| File                                               | Holds                                                                       | Updated by                                                       |
| -------------------------------------------------- | --------------------------------------------------------------------------- | ---------------------------------------------------------------- |
| `mise.toml`                                        | the dev **toolchain**: `node`, `pnpm`, `python`, `uv`, `deno`               | **by hand** (see [Bumping by hand](#bumping-by-hand))            |
| `package.json`                                     | npm deps (`^` ranges), `packageManager` (pnpm), `pnpm.overrides` (CVE pins) | Renovate (npm)                                                   |
| `pnpm-lock.yaml`                                   | resolved npm versions                                                       | generated from `package.json` (Renovate keeps it in sync)        |
| `requirements-dev.txt` / `requirements-docs.txt`   | Python dep **ranges** — the **source of truth**, incl. deliberate ceilings  | by hand for ceilings; Renovate refreshes within them             |
| `requirements-dev.lock` / `requirements-docs.lock` | resolved Python versions (uv-compiled)                                      | `mise run lock-update` locally; Renovate recompiles on its PRs   |
| `.github/workflows/*.yml`                          | action SHA pins (`@<sha> # vX`), `setup-*` version inputs                   | Renovate (github-actions) for the SHAs; toolchain inputs by hand |
| `renovate.json`                                    | **policy only** — who may bump what. Not a version source.                  | by hand                                                          |

## What's genuinely duplicated (and why it's safe)

A handful of **toolchain** versions appear in more than one file, because each tool reads its own config location and
all copies must agree:

| Tool           | Appears in                                    | Must match because                                                 |
| -------------- | --------------------------------------------- | ------------------------------------------------------------------ |
| `pnpm`         | `mise.toml` + `package.json` `packageManager` | the package manager you run must be one version                    |
| `python`       | `mise.toml` + workflow `setup-python`         | must match Decky Loader's embedded `libpython3.11`                 |
| `uv`           | `mise.toml` + workflow `setup-uv`             | the local resolver must equal the lock author (reproducible locks) |
| `node`, `deno` | `mise.toml` + workflows                       | local == CI                                                        |

These are the only real duplicates, and **Renovate is configured to never touch them** (excluded by dependency name in
`renovate.json`). So a bot can't bump one copy and desync the rest — they only move when **you** move them, together.

## Who bumps what — the auto-merge policy

Update PRs are opened by [Renovate](https://docs.renovatebot.com/) (`renovate.json`, the Renovate GitHub App).
Auto-merge uses GitHub's native auto-merge, so **the required CI checks are the gate** — a PR only merges itself when
everything is green.

| Category                                           | Auto-merges?                    |
| -------------------------------------------------- | ------------------------------- |
| Python (pip) in-range minor/patch + lock refreshes | ✅                              |
| npm / GitHub-Actions minor / patch / digest        | ✅                              |
| **Any major** (any ecosystem)                      | ❌ → human review               |
| **Toolchain** (`node`/`pnpm`/`python`/`uv`/`deno`) | ❌ not even proposed (excluded) |
| **Raising a deliberate ceiling** (see below)       | ❌ not even proposed            |

## Ceilings — versions deliberately held back

Two Python dev tools are capped tighter than "next major", because the next release is known-breaking:

| Dep              | Ceiling | Why                                                                                                   |
| ---------------- | ------- | ----------------------------------------------------------------------------------------------------- |
| `pytest-asyncio` | `<1.4`  | 1.4 changed event-loop semantics ([#806](https://github.com/danielcopper/decky-romm-sync/issues/806)) |
| `ruff`           | `<0.16` | 0.x minors ship new lint rules (breaking)                                                             |

The ceiling lives in `requirements-dev.txt`. Renovate's `rangeStrategy: update-lockfile` refreshes the lock _within_ the
range, but it does **not** stop a range-widening PR when a newer version is _above_ the ceiling — so `renovate.json`
also carries an `allowedVersions` cap for each of these two deps to keep Renovate from proposing the raise at all.

**Heads up — these two ceilings are duplicated:** the `<1.4` / `<0.16` caps exist in **both** `requirements-dev.txt` and
`renovate.json` (`allowedVersions`). If you deliberately raise a ceiling, change it in **both** places (the
`renovate.json` rules are commented `KEEP IN SYNC`). This is the only version duplication Renovate forces on us.

## Bumping by hand

- **Toolchain** (`mise.toml`): edit the pin, then update every coupled copy in the same commit — `package.json`
  `packageManager` for pnpm, the workflow `setup-*` inputs for python/uv/node/deno. Run `mise install` to pick it up.
- **A Python ceiling**: raise the `<X` in `requirements-*.txt`, raise the matching `allowedVersions` in `renovate.json`,
  run `mise run lock-update`, commit together.
- **Python locks** after any `requirements-*.txt` edit: `mise run lock-update` (the `check_lock_sync` CI gate enforces
  this).
