# Development

Guide for setting up a development environment and contributing to decky-romm-sync.

## Prerequisites

- [mise](https://mise.jdx.dev/) — manages Node, pnpm, and Python versions
- Git
- A Steam Deck or Linux PC with [Decky Loader](https://decky.xyz/) installed (for testing)

## Setup

```bash
git clone https://github.com/danielcopper/decky-romm-sync.git
cd decky-romm-sync
mise install          # installs Node LTS, pnpm, Python
mise run setup        # installs JS + Python dependencies
```

This creates a Python virtual environment (auto-activated by mise via `_.python.venv` in `mise.toml`) and installs all npm packages.

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

Tests mirror the source layout (`tests/services/`, `tests/adapters/`, `tests/domain/`, `tests/models/`, `tests/lib/`), with each test file mapping 1:1 to a source module. Shared mocks live in `tests/conftest.py`, which also provides a mock `decky` module so tests run without Decky Loader.

Frontend component tests run with `mise run test:frontend` (`pnpm test`); see the CLAUDE.md "Frontend component tests" section for the `@decky/api` event harness.

Every backend feature or callable where testing makes sense should have unit tests covering:

- **Happy path** — normal successful operation
- **Bad path** — invalid input, missing data, API errors, network failures
- **Edge cases** — empty strings, None values, boundary conditions

## Dev Reload

```bash
mise run dev          # builds frontend + restarts plugin_loader
```

This runs `pnpm build` and then `sudo systemctl restart plugin_loader` to pick up changes. For backend-only changes, restarting the plugin loader is sufficient without rebuilding.

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
- Services must not import stdlib I/O / non-deterministic primitives (`time`, `uuid`, `random`, `subprocess`, `threading`, `requests`)
- Services must be independent of each other (no cross-service imports)

`mise run lint` also runs `scripts/check_cosmic_call_bans.sh`, which complements the import rules at the call site: services may not call `datetime.now()` / `asyncio.sleep()` / `time.time()` / `time.monotonic()` / `uuid.uuid4()` / `random.*` directly — they inject the `Clock` / `Sleeper` / `UuidGen` Protocol instead.

See [Backend Architecture](../architecture/backend-architecture.md) for details.

## Code Quality

- **SonarCloud** — CI-based analysis on every PR and push to main. Quality Gate enforces 80% coverage on new code, 0 bugs, 0 vulnerabilities.
- **Ruff** — Python linting in CI. Expanded ruleset includes B (bugbear), SIM (simplify), UP (pyupgrade), RUF (ruff-specific), and ARG (unused arguments) in addition to the base E/F rules.
- **basedpyright** — Type checking in CI. Checks all source files including the test suite (tests/ is not excluded).
- **import-linter** — Layer boundary enforcement in CI (see Linting section above).
- **pytest-cov** — Branch coverage reported to SonarCloud.

## Project Structure

```text
main.py                              # Plugin entry — Decky lifecycle + callable surface
py_modules/
  bootstrap.py                       # Composition root — bootstrap() builds adapters, wire_services() builds services
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
    persistence.py / registry_store.py / metadata_cache_store.py
    download_file.py / download_queue.py / firmware_file.py / migration_file.py / rom_files.py / save_file.py
    retrodeck_paths.py / retroarch_config.py / retroarch_core_info.py / es_de_config.py
    system_clock.py / system_uuid_gen.py / asyncio_sleeper.py / hostname.py / path_probe.py / plugin_metadata.py / debug_logger.py
  domain/                            # Pure compute — no I/O, no service/adapter imports
    sync_action.py / sync_diff.py / preview_delta.py / work_unit.py
    save_state.py / save_path.py / save_status*.py / save_attribution.py / save_extensions.py
    firmware_paths.py / bios.py / achievements.py / shortcut_data.py / steam_categories.py
    sgdb_artwork.py / installed_roms.py / rom_files.py / retroarch_core_info.py
    state_migrations.py / sync_state.py / emulator_tag.py / version.py
  models/                            # Data shapes (TypedDicts/dataclasses) — independent of other layers
  lib/                               # Cross-cutting utilities (errors, list_result, iso_time, path_safety, late_binding, ...)
src/                                 # Frontend TypeScript
  index.tsx                          # Plugin entry, event listeners, QAM router
  components/                        # React components (QAM pages, game detail UI)
  patches/                           # Route and store patches
  api/backend.ts                     # callable() wrappers (typed)
  types/                             # TypeScript interfaces and Steam API declarations
  utils/                             # Shortcut CRUD, sync, downloads, collections, session manager
bin/romm-launcher                    # Bash launcher for RetroDECK
defaults/config.json                 # platform_map: 149 platform slug -> RetroDECK system mappings
tests/                               # Backend unit tests, mirroring py_modules/ layout
```

See [Backend Architecture](../architecture/backend-architecture.md) for the service/adapter design, dependency diagram, and layer enforcement rules.
