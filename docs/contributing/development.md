# Development

Guide for setting up a development environment and contributing to decky-romm-sync.

## Prerequisites

- [mise](https://mise.jdx.dev/) — manages Node, pnpm, and Python versions
- Git
- A Steam Deck or Linux PC with [Decky Loader](https://decky.xyz/) installed (for testing)

> **On Windows, develop inside [WSL2](https://learn.microsoft.com/windows/wsl/install).** The plugin targets Linux —
> some adapters import Unix-only modules (e.g. `fcntl`), a few dev dependencies have no Windows wheel, and CI runs on
> Linux. Native Windows is not supported for running the test suite; in a WSL2 Linux distro the same `mise install` /
> `mise run setup` / `mise run test` work unchanged.

## Setup

```bash
git clone https://github.com/danielcopper/decky-romm-sync.git
cd decky-romm-sync
mise install          # installs Node LTS, pnpm, Python
mise run setup        # installs JS + Python dependencies
```

This creates a Python virtual environment (auto-activated by mise via `_.python.venv` in `mise.toml`) and installs all
npm packages.

Python dependencies are installed from `requirements-dev.lock` — fully-pinned versions compiled from
`requirements-dev.txt` by uv. After changing a source (`requirements-dev.txt` / `requirements-docs.txt`) or bumping a
pin, run `mise run lock-update` to regenerate the locks.

### Automated dependency updates

Update PRs are managed by [Renovate](https://docs.renovatebot.com/) (`renovate.json`) across pip, npm, and GitHub
Actions, and in-range minor/patch updates auto-merge once CI is green. For the full picture — **where every version
lives, what's coupled, what auto-merges, and how to bump things by hand** — see
[Dependency management](dependency-management.md).

The toolchain versions — `node`, `pnpm`, `python`, `uv`, `deno` — are **excluded** from Renovate: they are pinned and
cross-file-coupled (each appears in `mise.toml` and in `package.json`'s `packageManager` and/or the workflow `setup-*`
version inputs, and all copies must match — `python` to Decky's embedded libpython3.11, `uv` for lock reproducibility).
Renovate is disabled for these by dependency name so a bot bump can't desync one copy; bump them by hand, together. The
`setup-*` action SHAs themselves stay auto-updated.

## Building

```bash
pnpm build            # Rollup -> dist/index.js
```

The frontend is bundled with Rollup into a single `dist/index.js` file that Decky Loader serves.

## Testing

```bash
python -m pytest tests/ -q     # run the backend test suite
mise run test                   # same thing via mise
```

To run with coverage:

```bash
python -m pytest tests/ -q --cov=py_modules --cov=main --cov-report=term --cov-branch
```

Tests mirror the source layout (`tests/services/`, `tests/adapters/`, `tests/domain/`, `tests/models/`, `tests/lib/`),
with each test file mapping 1:1 to a source module. Shared mocks live in `tests/conftest.py`, which also provides a mock
`decky` module so tests run without Decky Loader.

Frontend component tests run with `mise run test:frontend` (`pnpm test`); see `.claude/rules/testing-frontend.md` for
the `@decky/api` event harness.

### Property-based tests

The pure decision kernels carry an extra tier of [Hypothesis](https://hypothesis.readthedocs.io/) property tests
alongside the hand-enumerated cases. They state the safety invariants directly and exercise them across a generated
input space. The in-tree kernels (`domain/save_path.py`, `domain/iso_time.py`) have theirs in
`tests/domain/test_*_property.py`; the save-sync decision runs in the compiled gavel core, so its properties drive
`GavelNativeAdapter` from `tests/adapters/test_gavel_native_property.py`. Run them like any other test:

```bash
python -m pytest tests/adapters/test_gavel_native_property.py tests/domain/test_save_path_property.py -q
```

Hypothesis is a dev-only dependency (pinned in `requirements-dev.txt`, compiled into `requirements-dev.lock` via
`mise run lock-update` — it never ships in the plugin). A CI-safe profile in `tests/conftest.py` sets `deadline=None`
(no timing flakes on shared runners) and a fixed example count. The example database is written to `.hypothesis/`, which
is gitignored. See `.claude/rules/testing-backend.md` for the convention on pinning a property that encodes an open bug.

### Contract tests

`tests/contract/` is a tier that crosses the frontend↔backend wire. Where the unit tests check each side against its own
mocked idea of the other, the contract tier builds the **real** `Plugin` through the **real** `bootstrap()` +
`wire_services()` (real settings dict, real SQLite + migrations, real file-store adapters, all under `tmp_path`) and
drives the actual `main.py` callables **exactly as the frontend does** — positional, JSON-shaped arguments with the arg
types declared in `src/api/backend.ts` (literal `None` where the TS type says `null`). The assertions pin the response
_shape_ (canonical failure shape, discriminated-status unions, partial-success flags), not delegation. Only the
outermost edges are faked: the RomM + SteamGridDB network transports, the Clock/UuidGen/Sleeper seams, `emit`, and the
retry backoff. Run them like any other test:

```bash
python -m pytest tests/contract/ -q
```

A `backend.ts` manifest gate (Phase 2) that pins the frontend and backend to one parsed artifact is a forthcoming
separate change. See `.claude/rules/testing-backend.md` for the full contract-tier rules.

### Gavel conformance vectors

The save-sync decisions are also published as a standalone client contract,
[romm-gavel](https://github.com/danielcopper/romm-gavel) — and since both of them run in gavel's compiled core (vendored
as `py_modules/native/libgavel-x86_64-linux.so`), the two vector families are what keep the shipped binary and the
published spec from silently drifting apart:

- **ladder** (the 409 resolution ladder) — `tests/adapters/test_gavel_native.py`.
- **decision-table** (the full per-`(rom, filename, slot)` decision) —
  `tests/adapters/test_gavel_native_table_vectors.py`.

Both families read the core through `GavelNativeAdapter`, the seam production decides on. The vectors are vendored
verbatim under `tests/domain/gavel_vectors/`, one subdirectory per family mirroring upstream `vectors/` (`ladder/` — a
curated named-case set plus the exhaustive equivalence classes; `decision-table/` — curated named cases) — there is no
submodule and no network in CI, so every contract change lands as a reviewable diff. Run them like any other test:

```bash
python -m pytest tests/adapters/test_gavel_native_table_vectors.py tests/adapters/test_gavel_native.py -q
```

Updating the vectors means deliberately re-copying the JSON from the matching upstream `vectors/<family>/` directory and
bumping the release tag in `tests/domain/gavel_vectors/README.md` — in lockstep with the `.so`, which is pinned to the
same release; never edit a vector to match the core.

### emu-atlas conformance vectors

The config-aware emulator knowledge — where a RetroArch / RetroDECK install keeps its saves — is likewise published as a
standalone library, [emu-atlas](https://github.com/danielcopper/emu-atlas), extracted from this plugin. Its `machines`
vector family (16 fixture machines in, detected installations + save placements out) runs against the plugin's own
save-path kernel in `tests/test_atlas_machine_vectors.py`, so the two can't silently drift. Each vector materializes a
`{path: content}` file tree under a `tmp_path` fake home, then drives the real adapters (`RetroDeckPathsAdapter` +
`RetroArchConfigAdapter`) and the domain save-path functions (`resolve_save_dir` / `compute_local_save_target`).

The overlap is partial, so every vector carries an explicit check level (an `_CHECK_LEVELS` allowlist entry that also
records _why_):

- **`full`** — end-to-end placement. The plugin derives the saves root the same way atlas does (from `retrodeck.json`,
  or the `~/retrodeck` fallback), so the final directory + filename strings are compared. Covers the RetroDECK-flavor
  `InSaveDir` cases and the RetroDECK-first coexistence case.
- **`layout-only`** — only the `retroarch.cfg` interpretation overlaps. The plugin has no standalone-RetroArch
  saves-root concept (its saves base always comes from RetroDECK paths), so a vector whose placement hangs off a
  standalone `savefile_directory` is checked on the `SaveLayout` the plugin derives from the same cfg text — the sort
  flags for an `InSaveDir` placement, or the `ContentDir` (next-to-ROM) classification.
- **`n/a`** — no overlap (the plugin has no installation-enumeration surface, so atlas's "nothing detected" outcome has
  no plugin equivalent). The check only guards that the vector stays in its non-checkable shape.

No vector is silently skipped: a new upstream vector without an allowlist entry (or a stale entry for a removed one)
fails at collection. The vectors are vendored verbatim under `tests/atlas_vectors/machines/` at a pinned upstream
release tag — no submodule, no network in CI. Run it like any other test:

```bash
python -m pytest tests/test_atlas_machine_vectors.py -q
```

Updating means deliberately re-copying the JSON from upstream `vectors/machines/` and bumping the release tag in
`tests/atlas_vectors/README.md`; never edit a vector to match the kernel.

Every backend feature or callable where testing makes sense should have unit tests covering:

- **Happy path** — normal successful operation
- **Bad path** — invalid input, missing data, API errors, network failures
- **Edge cases** — empty strings, None values, boundary conditions

## Dev Reload

```bash
mise run dev          # build frontend, deploy to the plugin dir, restart plugin_loader
mise run dev dp2      # ...and also open windowed Big Picture on that display after deploying
```

This builds the frontend, copies the plugin files into `~/homebrew/plugins/decky-romm-sync`, and restarts
`plugin_loader` to pick up the changes. It **stops** `plugin_loader` around the file copy on purpose: the loader runs as
root and continuously re-owns the plugin dir back to root within ~1–2s as a tamper guard, so copying while it runs races
against that re-own and fails with `permission denied`. With the loader stopped, the copy is uncontested; it restarts
automatically when the task finishes — even if the build or copy fails, so a failure never leaves the plugin dead. For
backend-only changes, restarting the plugin loader is sufficient without rebuilding.

Passing a display target (`internal`, or an output name like `dp2` / `DP-3` — the same argument
[`dev:watch`](frontend-dev-loop.md#choosing-the-display) takes) also opens a windowed Big Picture on that display once
the deploy succeeds, so you can deploy and eyeball the result in one command. With no argument, `dev` stays deploy-only
and never opens a window. A bad display name is rejected up front, before the loader is stopped.

For frontend iteration there is a much faster loop: after a one-time `mise run dev:setup`,
`mise run dev:watch [display]` hot-reloads the **frontend** into a windowed Big Picture on the desktop as you save, with
no loader restarts at all — put it on a second monitor with a display target like `dp2`. Backend changes are pushed on
demand with `mise run dev:push-backend`. That windowed Big Picture gives the QAM panel ~59% more vertical room than the
Deck does, so judge layout and overflow under `mise run dev:ui-scale`, which forces Steam's display scale to Game
Mode's. See [Frontend dev loop](frontend-dev-loop.md) for the full workflow, keyboard shortcuts, and caveats.

## Deploying to Device

For development, symlink the repo into the plugins directory:

```bash
sudo ln -sf "$(pwd)" ~/homebrew/plugins/decky-romm-sync
sudo systemctl restart plugin_loader
```

This way, rebuilds take effect immediately after a Decky restart.

## Linting

```bash
PYTHONPATH=py_modules lint-imports   # check service/adapter layer rules
mise run lint                        # same via mise
```

The `.importlinter` config enforces the layer boundary contracts:

- Services must not import concrete adapter implementations (Protocols are allowed)
- Adapters must not import services
- Utilities (`lib/`) must not import services, adapters, or domain
- Domain must not import services or adapters (`lib` is allowed)
- Models must not import services, adapters, domain, or lib
- Services must not import stdlib I/O / non-deterministic primitives (`time`, `uuid`, `random`, `subprocess`,
  `threading`, `requests`)
- Services must be independent of each other (no cross-service imports)

`mise run lint` also runs `scripts/check_cosmic_call_bans.sh`, which complements the import rules at the call site:
services may not call `datetime.now()` / `asyncio.sleep()` / `time.time()` / `time.monotonic()` / `uuid.uuid4()` /
`random.*` directly — they inject the `Clock` / `Sleeper` / `UuidGen` Protocol instead.

`mise run lint` (and CI) also runs `scripts/check_service_independence_contract.py`, which derives the expected service
list from `py_modules/services/` and fails if `.importlinter`'s `service-independence` contract drifts — omitting a
service or carrying a stale entry — keeping the hand-maintained `modules` list self-healing.

`mise run lint` (and CI) also runs `scripts/check_failure_shape.py --check`, which fails if any `success: False` return
in `services/` is missing the canonical `reason` + `message` keys or carries the forbidden `error` / `error_code` key —
collapsing the failure-shape dialects onto one vocabulary (the two documented carve-outs are pattern-exempt). Run it
without `--check` for a report-mode inventory.

`mise run lint` (and CI) also runs `scripts/check_callable_manifest.py`, which pins the frontend↔backend callable
surface to one source of truth: it derives the frontend names + arities from every `callable<[Args], Return>("name")` in
`src/**/*.ts` and the backend surface from the public `async def` methods on the `Plugin` class in `main.py`, then fails
if they diverge — a callable declared on only one side (either direction) or a matching name whose arity (positional
param count) differs. Arg types stay out of scope (Python signatures carry no hints), so arity is the only mechanically
checkable shape. The same parity assertion is surfaced inside the pytest run by
`tests/contract/test_callable_manifest.py`.

`mise run lint` (and CI) also runs `scripts/check_event_parity.py`, which fails if a backend `emit("name", ...)` event
has no matching frontend `addEventListener("name", ...)` (or vice versa). The event names are bare string literals, so
the gate matches the two surfaces by literal event name — the backend side parsed via AST (`emit` / `_emit` calls), the
frontend side via a text scan of bare `addEventListener` calls. Static sibling of the callable-manifest gate, for the
event channel. The same parity assertion is surfaced inside the pytest run by `tests/contract/test_event_parity.py`.

`mise run lint` (and CI) also runs `scripts/check_settings_owner.py`, which fails if the `settings.json` filename
literal appears anywhere except its owning adapter (`adapters/persistence.py`); confining the literal to one module
keeps all settings writes in the single crash-safe owner.

`mise run lint` (and CI) also runs `scripts/check_module_size.py`, the decomposition-threshold ratchet: no module in
`services/`, `bootstrap/`, `adapters/`, `domain/`, `lib/` or `models/` may cross the ~1000-LOC threshold, and the
modules that were already over it when the gate landed are pinned at their exact size, so they cannot grow. The pin list
lives in the script and only ever gets shorter — a module that drops back under the threshold has to leave it, and a
module that banks 50+ lines of slack gets a non-fatal note asking for its ceiling to be lowered. What the gate does not
walk is listed at `SCOPE_DIRS` with the reason for each: `main.py` grows with the callable surface by design, `_vendor/`
is a checksum-pinned copy, a large file under `tests/` is the one-file-per-source-module rule working, `scripts/` never
ships, and `src/` needs a per-scope glob before it can be added. There is deliberately no `--update` flag —
re-baselining should be a reviewable diff, never a command someone runs to get back to green.

The frontend has no size gate — deliberately, because a threshold only works when something else forbids the cheap way
of getting under it, and `src/` has no equivalent of `service-independence`. What it has instead is direction rules, in
`eslint.config.js` via `eslint-plugin-import-x`: `src/utils/` and `src/api/` may not import `src/components/`, and no
module in `src/` may take part in an import cycle. The cycle rule is the one that matters most, because a cycle is the
signature of a split whose two halves still call each other — the wrong seam, detectable without judgment. What none of
them catch is a helper imported by exactly one parent that takes a dozen parameters and does nothing on its own: it is
neither a cycle nor a direction violation. These rules make the worst seam fail; they do not certify that a seam is
right.

Two settings in that config are load-bearing and neither is the plugin's default. `import-x/extensions` ships as
`['.js']`, so until it names `.ts`/`.tsx` the plugin resolves an import but never opens the target file to read _its_
imports — `no-cycle` then walks a graph one edge deep and reports nothing, on any codebase. `import-x/parsers` supplies
the parser it needs for that reading. Because the failure mode is silence rather than noise,
`src/eslintBoundaries.test.ts` lints known-bad fixtures through the real config and fails if any of the three rules
stops reporting. A green `pnpm lint` on its own does not distinguish a working rule from an inert one.

See [Backend Architecture](../architecture/backend-architecture.md) for details.

## Full CI gate

```bash
mise run gate         # run every PR check from .github/workflows/ci.yml, locally
```

`mise run gate` is the single local battery that mirrors CI. It runs the backend tests (`mise run test`) and the
architecture/lint gates (`mise run lint`), then adds the rest of what CI enforces: `ruff check` + `ruff format --check`,
`basedpyright`, the frontend `eslint` / `prettier --check` / build / `tsc` typecheck / bundle-size budget, the frontend
tests (`pnpm test`), and `deno fmt --check` for Markdown. It is slow — a full pytest run plus a production frontend
build — so it is a pre-push check, not something to run on every save. The only CI jobs it can't reproduce are the
SonarCloud scan and its `sonar-gate` (they need `SONAR_TOKEN` and the CI coverage artifacts).

## Code Quality

- **SonarCloud** — CI-based analysis on every human PR and push to main. Quality Gate enforces 80% coverage on new code,
  0 bugs, 0 vulnerabilities. The scan is skipped on Dependabot PRs (no `SONAR_TOKEN` access in that restricted context);
  the required status check is the `sonar-gate` job, which passes when SonarCloud succeeded or was skipped and fails
  only when it failed — so dependency PRs aren't deadlocked on a check that can never run for them.
- **Ruff** — Python linting in CI. Expanded ruleset includes B (bugbear), SIM (simplify), UP (pyupgrade), RUF
  (ruff-specific), and ARG (unused arguments) in addition to the base E/F rules.
- **basedpyright** — Type checking in CI. Checks all source files including the test suite (tests/ is not excluded).
- **import-linter** — Layer boundary enforcement in CI (see Linting section above).
- **pytest-cov** — Branch coverage reported to SonarCloud.

## Where the coding conventions live

Two files, split by how often they apply:

- **`CLAUDE.md`** (repo root) — the traps, the cross-cutting invariant register, and the workflow. Everything here
  applies no matter which file you touch, so it is read up front.
- **`.claude/rules/*.md`** — the per-area conventions (services, adapters/domain, Python naming and docstrings, callable
  shapes, bootstrap wiring, vendored assets, backend and frontend testing). Each file carries a `paths:` frontmatter
  glob and is loaded when a matching file is opened, which keeps the always-on set small.

Most of what lives in `.claude/rules/` has no mechanical check — Protocol suffixes, constructor shape, docstring intent,
verb-named mutations — so those rules hold only if they are carried while writing. `CLAUDE.md` indexes them with the
failure mode each one prevents.

Note that `.gitignore` ignores `.claude/*` (worktrees, local agents, `settings.local.json`) and re-includes
`.claude/rules/` explicitly. Adding a rule file works; adding anything else under `.claude/` will silently not be
tracked.

## Project Structure

```text
main.py                              # Plugin entry — Decky lifecycle + callable surface
py_modules/
  bootstrap/                         # Composition root — re-exported through __init__.py
    adapters.py                      # bootstrap() builds every adapter and the typed bundles
    services.py                      # wire_services() builds every service from those bundles
  services/                          # Orchestration / business logic (Protocol-typed deps via *ServiceConfig)
    protocols/                       # Protocol interfaces, grouped: transport / determinism /
                                     #   persistence / paths / infra / files / cross_service
    library/                         # LibraryService façade — fetcher, sync_orchestrator, reporter, shared state box
    saves/                           # SaveService aggregate — state, sync_engine/, slots/, status/, versions
    downloads.py                     # DownloadService — ROM downloads, ZIP/M3U, fcntl queue
    firmware.py                      # FirmwareService — BIOS registry + downloads
    session_lifecycle.py             # SessionLifecycleService — post-exit orchestration
    migration.py                     # MigrationService — RetroDECK path + save-sort migration
    steamgrid.py                     # SteamGridService — SteamGridDB artwork
    artwork.py                       # ArtworkService — cover art staging/cleanup
    game_detail.py / playtime.py / achievements.py / settings.py / cores.py
    metadata.py / rom_removal.py / shortcut_removal.py / launch_gate.py
    startup_healing.py / connection.py
  adapters/                          # I/O boundaries — implement Protocols
    romm/{http,romm_api}.py          # RomM HTTP transport + REST adapter
    steam_config.py / steamgriddb.py / sgdb_artwork_cache.py / cover_art_file_store.py
    persistence.py                   # settings.json read/write + one-time legacy save_sync_state fold
    repositories/                    # SqliteUnitOfWork (unit_of_work.py) + 9 repos (8 aggregate + kv_config)
                                     #   (rom, rom_install, rom_metadata, playtime, rom_save_sync_state,
                                     #    bios_file, firmware_cache, sync_run, kv_config)
    sqlite_migrations.py / machine_id.py  # schema migration runner (PRAGMA user_version) + machine-id reader
    download_file.py / firmware_file.py / migration_file.py / rom_files.py / save_file.py
    retrodeck_paths.py / retroarch_config.py / retroarch_core_info.py / es_de_config.py
    system_clock.py / system_uuid_gen.py / asyncio_sleeper.py / hostname.py / path_probe.py / plugin_metadata.py / debug_logger.py
  db/
    migrations/001_initial.sql       # SQLite schema DDL
  domain/                            # Pure compute — no I/O, no service/adapter imports
    _aggregate.py                    # the @cosmic_aggregate decorator
    rom.py / rom_install.py / rom_metadata.py / rom_metadata_mapping.py / playtime.py
    rom_save_sync_state.py / bios_file.py / firmware_cache.py / sync_run.py
    sync_action.py / sync_diff.py / preview_delta.py / work_unit.py
    save_path.py / save_status*.py / save_attribution.py / save_extensions.py
    firmware_paths.py / bios.py / achievements.py / shortcut_data.py / steam_categories.py
    sgdb_artwork.py / installed_roms.py / rom_files.py / retroarch_core_info.py
    state_migrations.py / sync_state.py / emulator_tag.py / version.py
  models/                            # Data shapes (TypedDicts/dataclasses) — independent of other layers
  lib/                               # Cross-cutting utilities (errors, list_result, iso_time, path_safety, late_binding, ...)
  _vendor/                           # Vendored third-party deps — not our code, only imported by adapters
    README.md                        # Provenance per package: upstream URL, version/commit, local patches
    vdf/                             # Valve Data Format parser (Steam shortcuts.vdf)
      LICENSE                        # Upstream MIT license — preserved on redistribution
src/                                 # Frontend TypeScript
  index.tsx                          # Plugin entry, event listeners, QAM router
  components/                        # React components (QAM pages, game detail UI)
  patches/                           # Route and store patches
  api/backend.ts                     # callable() wrappers (typed)
  types/                             # TypeScript interfaces and Steam API declarations
  utils/                             # Shortcut CRUD, sync, downloads, collections, session manager
bin/rom-launcher                     # Pure exec wrapper — runs the launch command baked into the shortcut
defaults/config.json                 # platform_map: 150 platform slug -> RetroDECK system mappings
tests/                               # Backend unit tests, mirroring py_modules/ layout
```

See [Backend Architecture](../architecture/backend-architecture.md) for the service/adapter design, dependency diagram,
and layer enforcement rules.
