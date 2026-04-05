# Test Coverage & Methodology Analysis

> **Date:** April 1, 2026
> **Scope:** Full audit of the decky-romm-sync test suite — structure, methodology, coverage gaps, and improvement recommendations.

---

## Executive Summary

decky-romm-sync has an **exceptionally strong test suite** by open-source plugin standards: **1,721 test functions** across **52 test files**, covering a production codebase of ~12,400 Python lines and ~9,100 TypeScript lines. The test-to-source ratio is **2.2× more test code than production code** on the Python side. Architecture boundaries are enforced by 6 import-linter contracts, and CI runs tests, branch coverage, type checking (basedpyright), linting (Ruff), and SonarCloud quality gates on every PR.

That said, this audit identifies **concrete, actionable gaps** — most notably zero frontend tests, no integration/E2E tests, a few untested source modules, and underuse of parameterized testing. The sections below provide a module-by-module coverage map, pattern analysis, and prioritized improvement plan.

---

## Table of Contents

1. [Test Infrastructure](#1-test-infrastructure)
2. [Source Module Inventory](#2-source-module-inventory)
3. [Test File Inventory](#3-test-file-inventory)
4. [Coverage Map: Source → Tests](#4-coverage-map-source--tests)
5. [Uncovered Source Modules](#5-uncovered-source-modules)
6. [Test Methodology Analysis](#6-test-methodology-analysis)
7. [Frontend Test Gap](#7-frontend-test-gap)
8. [Cross-Platform Portability](#8-cross-platform-portability)
9. [Improvement Recommendations](#9-improvement-recommendations)
10. [Priority Matrix](#10-priority-matrix)

---

## 1. Test Infrastructure

### Test Runner & Configuration

| Component | Value |
|---|---|
| Framework | pytest + pytest-asyncio |
| Async mode | `auto` (all `async def test_*` auto-collected) |
| Import mode | `importlib` (avoids `__init__.py` requirements) |
| Coverage tool | pytest-cov with `--cov-branch` |
| Coverage scope | `py_modules/` + `main.py` |

### CI Pipeline (`.github/workflows/ci.yml`)

| Job | What it does |
|---|---|
| **test** | Python 3.12, `pytest --cov-branch`, uploads `coverage.xml` |
| **build** | Node.js LTS + pnpm 9, `pnpm build` (TypeScript compilation only) |
| **lint** | `ruff check .` + `basedpyright` + `lint-imports` (6 architecture contracts) |
| **sonarcloud** | Downloads coverage artifact → SonarCloud quality gate |

**Triggers:** push to `main`, all PRs to `main`.

### Static Analysis

| Tool | Purpose |
|---|---|
| **Ruff** | Linting: pycodestyle, pyflakes, isort, bugbear, pyupgrade, unused args |
| **basedpyright** | Type checking (basic mode, Python 3.12) |
| **import-linter** | 6 architecture boundary contracts (see below) |
| **SonarCloud** | Continuous inspection, quality gate, coverage tracking |

### Architecture Boundary Contracts (`.importlinter`)

| Contract | Rule |
|---|---|
| no-adapter-impl-in-services | Services → adapters (concrete) **forbidden** (protocols OK) |
| no-services-in-adapters | Adapters → services **forbidden** |
| utilities-independence | `lib/` → services, adapters, domain **forbidden** |
| domain-independence | `domain/` → services, adapters, lib **forbidden** |
| models-independence | `models/` → services, adapters, domain, lib **forbidden** |
| service-independence | Each service module is **independent** of every other service |

This enforces a strict layered architecture: `models → domain → lib → adapters → services`, with services communicating only through protocol interfaces defined in `services/protocols.py`.

### Global Test Fixtures (`tests/conftest.py`)

- **`_DeckyMock`**: Custom `MagicMock` subclass that intercepts `DECKY_USER_HOME` and `DECKY_PLUGIN_DIR` assignments to auto-reconfigure domain modules (retrodeck_config, es_de_config). Injected as `sys.modules["decky"]` before any test imports.
- **`_reset_retrodeck_config_user_home`** (autouse): Resets module-level state between every test with fresh temp dirs — prevents cross-test pollution.
- **`_make_testable_plugin()`**: Factory that creates a `TestablePlugin` subclass with typed test-only attributes (`_fake_api`, `_resolve_system`).

---

## 2. Source Module Inventory

### Production Python (~12,357 lines)

| Directory | Files | Lines | Purpose |
|---|---|---|---|
| `py_modules/adapters/` | 5 | 596 | Persistence, RetroDECK config, Steam config, SteamGridDB client |
| `py_modules/adapters/romm/` | 5 | 690 | RomM HTTP client, API routing, v4.6/v4.7 implementations |
| `py_modules/domain/` | 14 | 2,101 | Core business logic: BIOS, ES-DE, save sync, shortcuts, categories |
| `py_modules/lib/` | 4 | 594 | Utilities: errors, perf metrics, cert bundle, adaptive semaphore |
| `py_modules/models/` | 4 | 135 | Data classes: BIOS, metadata, saves |
| `py_modules/services/` | 15 | 7,213 | Service layer: 14 services + protocol definitions |
| `py_modules/bootstrap.py` | 1 | 349 | Composition root (dependency injection wiring) |
| `main.py` | 1 | 679 | Plugin entry point (Decky Loader facade) |
| **Total** | **49** | **12,357** | |

### Frontend TypeScript/React (~9,089 lines)

| Directory | Files | Lines | Purpose |
|---|---|---|---|
| `src/components/` | 12 | ~6,000 | React UI components (LibraryPage, SettingsPage, modals, etc.) |
| `src/api/` | 1 | ~350 | Backend API bindings |
| `src/utils/` | 13 | ~2,200 | State stores, formatters, sync managers, style injection |
| `src/patches/` | 2 | ~350 | Monkey-patches for Decky/Steam UI |
| `src/types/` | 1 | ~90 | TypeScript type definitions |
| `src/index.tsx` | 1 | ~100 | Plugin entry point |
| **Total** | **31** | **~9,089** | |

---

## 3. Test File Inventory

**52 test files, 1,721 test functions, ~26,704 lines of test code**

### Root-level tests (`tests/`)

| File | Tests | Lines | Covers |
|---|---|---|---|
| `test_bootstrap.py` | 9 | 170 | `bootstrap.py` — DI wiring, component types, path injection |
| `test_plugin.py` | 51 | 685 | `main.py` — settings, connection, log levels, pruning, atomics |
| `test_plugin_saves.py` | 43 | 832 | `main.py` — save sync, playtime, conflict resolution via Plugin |

### Adapter tests (`tests/adapters/`)

| File | Tests | Lines | Covers |
|---|---|---|---|
| `test_persistence.py` | 34 | 319 | `adapters/persistence.py` — file I/O, atomics, crash safety |
| `test_retrodeck_config.py` | 15 | 114 | `adapters/retrodeck_config.py` — path detection, config parsing |
| `test_steam_config.py` | 42 | 415 | `adapters/steam_config.py` — shortcuts, grid art, user IDs |
| `test_steamgriddb.py` | 10 | 113 | `adapters/steamgriddb.py` — SteamGridDB HTTP client |
| `romm/test_api_freeze.py` | 1 | 19 | API contract stability (freeze test) |
| `romm/test_api_router.py` | 51 | 316 | `adapters/romm/api_router.py` — version routing |
| `romm/test_api_v46.py` | 26 | 227 | `adapters/romm/api_v46.py` — v4.6 API |
| `romm/test_api_v47.py` | 31 | 286 | `adapters/romm/api_v47.py` — v4.7+ API |
| `romm/test_http.py` | 111 | 1,192 | `adapters/romm/http.py` — SSL, auth, timeouts, URL encoding |

### Domain tests (`tests/domain/`)

| File | Tests | Lines | Covers |
|---|---|---|---|
| `test_bios.py` | 46 | 423 | `domain/bios.py` — BIOS file matching |
| `test_emulator_tag.py` | 12 | 57 | `domain/emulator_tag.py` — tag extraction |
| `test_es_de_config.py` | 39 | 625 | `domain/es_de_config.py` — ES-DE XML parsing, core resolution |
| `test_rom_files.py` | 27 | 196 | `domain/rom_files.py` — multi-disc, file handling |
| `test_save_conflicts.py` | 32 | 243 | `domain/save_conflicts.py` — conflict detection |
| `test_save_extensions.py` | 12 | 87 | `domain/save_extensions.py` — platform save extensions |
| `test_save_matching.py` | 26 | 361 | `domain/save_sync.py` (matching logic) |
| `test_save_path.py` | 22 | 199 | `domain/save_path.py` — path resolution |
| `test_save_status.py` | 11 | 67 | `domain/save_status.py` — sync status formatting |
| `test_save_sync.py` | 19 | 200 | `domain/save_sync.py` — sync decision engine |
| `test_shortcut_data.py` | 12 | 140 | `domain/shortcut_data.py` — Steam shortcut builders |
| `test_state_migrations.py` | 14 | 108 | `domain/state_migrations.py` — version migrations |
| `test_steam_categories.py` | 9 | 48 | `domain/steam_categories.py` — genre mapping |

### Library tests (`tests/lib/`)

| File | Tests | Lines | Covers |
|---|---|---|---|
| `test_adaptive_semaphore.py` | 22 | 329 | `lib/perf.py` — AdaptiveSemaphore |
| `test_errors.py` | 47 | 313 | `lib/errors.py` — error hierarchy |
| `test_perf.py` | 35 | 311 | `lib/perf.py` — metrics, ETA, reporting |

### Model tests (`tests/models/`)

| File | Tests | Lines | Covers |
|---|---|---|---|
| `test_bios.py` | 9 | 112 | `models/bios.py` — BIOS data classes |
| `test_metadata.py` | 5 | 62 | `models/metadata.py` — metadata data classes |
| `test_saves.py` | 10 | 143 | `models/saves.py` — save data classes |

### Service tests (`tests/services/`)

| File | Tests | Lines | Covers |
|---|---|---|---|
| `test_achievements.py` | 67 | 1,131 | `services/achievements.py` — RetroAchievements |
| `test_artwork.py` | 36 | 462 | `services/artwork.py` — artwork caching |
| `test_collection_cache.py` | 11 | 267 | `services/library.py` (collection cache aspect) |
| `test_concurrent_artwork.py` | 10 | 235 | `services/artwork.py` (concurrency) |
| `test_concurrent_fetch.py` | 17 | 411 | `services/library.py` (concurrent platform fetch) |
| `test_downloads.py` | 82 | 1,657 | `services/downloads.py` — download queue |
| `test_firmware.py` | 69 | 1,855 | `services/firmware.py` — BIOS/firmware management |
| `test_game_detail.py` | 40 | 943 | `services/game_detail.py` — detail page aggregation |
| `test_library.py` | 183 | 3,394 | `services/library.py` — sync orchestration (largest) |
| `test_metadata.py` | 30 | 601 | `services/metadata.py` — metadata caching |
| `test_migration.py` | 19 | 606 | `services/migration.py` — RetroDECK migration |
| `test_migration_save_sort.py` | 13 | 435 | `services/migration.py` (save-sort aspect) |
| `test_playtime.py` | 20 | 347 | `services/playtime.py` — playtime tracking |
| `test_progress_reporting.py` | 11 | 224 | `services/library.py` (progress reporting aspect) |
| `test_removal_guard.py` | 11 | 257 | `services/rom_removal.py` (safety guards) |
| `test_rom_removal.py` | 25 | 398 | `services/rom_removal.py` — ROM deletion |
| `test_saves.py` | 187 | 3,306 | `services/saves.py` — save sync engine (largest) |
| `test_shortcut_removal.py` | 21 | 371 | `services/shortcut_removal.py` — shortcut cleanup |
| `test_steamgrid.py` | 36 | 706 | `services/steamgrid.py` — SteamGridDB art |

### Support files

| File | Purpose |
|---|---|
| `fakes/fake_save_api.py` (302 lines) | In-memory fake implementing the full RomM save API contract |
| `fakes/__init__.py` | Package marker |
| `conftest.py` (93 lines) | Global fixtures, Decky mock, state reset |

---

## 4. Coverage Map: Source → Tests

### ✅ Fully Covered (dedicated test file exists)

| Source Module | Test File(s) | Test Count |
|---|---|---|
| `adapters/persistence.py` | `adapters/test_persistence.py` | 34 |
| `adapters/retrodeck_config.py` | `adapters/test_retrodeck_config.py` | 15 |
| `adapters/steam_config.py` | `adapters/test_steam_config.py` | 42 |
| `adapters/steamgriddb.py` | `adapters/test_steamgriddb.py` | 10 |
| `adapters/romm/api_router.py` | `adapters/romm/test_api_router.py` | 51 |
| `adapters/romm/api_v46.py` | `adapters/romm/test_api_v46.py` + `test_api_freeze.py` | 27 |
| `adapters/romm/api_v47.py` | `adapters/romm/test_api_v47.py` | 31 |
| `adapters/romm/http.py` | `adapters/romm/test_http.py` | 111 |
| `domain/bios.py` | `domain/test_bios.py` | 46 |
| `domain/emulator_tag.py` | `domain/test_emulator_tag.py` | 12 |
| `domain/es_de_config.py` | `domain/test_es_de_config.py` | 39 |
| `domain/rom_files.py` | `domain/test_rom_files.py` | 27 |
| `domain/save_conflicts.py` | `domain/test_save_conflicts.py` | 32 |
| `domain/save_extensions.py` | `domain/test_save_extensions.py` | 12 |
| `domain/save_path.py` | `domain/test_save_path.py` | 22 |
| `domain/save_status.py` | `domain/test_save_status.py` | 11 |
| `domain/save_sync.py` | `domain/test_save_sync.py` + `test_save_matching.py` | 45 |
| `domain/shortcut_data.py` | `domain/test_shortcut_data.py` | 12 |
| `domain/state_migrations.py` | `domain/test_state_migrations.py` | 14 |
| `domain/steam_categories.py` | `domain/test_steam_categories.py` | 9 |
| `lib/errors.py` | `lib/test_errors.py` | 47 |
| `lib/perf.py` | `lib/test_perf.py` + `lib/test_adaptive_semaphore.py` | 57 |
| `models/bios.py` | `models/test_bios.py` | 9 |
| `models/metadata.py` | `models/test_metadata.py` | 5 |
| `models/saves.py` | `models/test_saves.py` | 10 |
| `services/achievements.py` | `services/test_achievements.py` | 67 |
| `services/artwork.py` | `services/test_artwork.py` + `test_concurrent_artwork.py` | 46 |
| `services/downloads.py` | `services/test_downloads.py` | 82 |
| `services/firmware.py` | `services/test_firmware.py` | 69 |
| `services/game_detail.py` | `services/test_game_detail.py` | 40 |
| `services/library.py` | `services/test_library.py` + 3 others | 222 |
| `services/metadata.py` | `services/test_metadata.py` | 30 |
| `services/migration.py` | `services/test_migration.py` + `test_migration_save_sort.py` | 32 |
| `services/playtime.py` | `services/test_playtime.py` | 20 |
| `services/rom_removal.py` | `services/test_rom_removal.py` + `test_removal_guard.py` | 36 |
| `services/saves.py` | `services/test_saves.py` | 187 |
| `services/shortcut_removal.py` | `services/test_shortcut_removal.py` | 21 |
| `services/steamgrid.py` | `services/test_steamgrid.py` | 36 |
| `bootstrap.py` | `test_bootstrap.py` | 9 |
| `main.py` (Plugin) | `test_plugin.py` + `test_plugin_saves.py` | 94 |

---

## 5. Uncovered Source Modules

| Source Module | Lines | Risk | Notes |
|---|---|---|---|
| `adapters/romm/api_base.py` | 26 | 🟢 Low | Abstract base class — tested indirectly via `api_v46.py` and `api_v47.py` |
| `domain/sync_state.py` | 9 | 🟢 Low | Trivial 3-value enum (`IDLE`, `RUNNING`, `CANCELLING`). Used extensively in other tests |
| `lib/certifi_bundle.py` | 18 | 🟡 Medium | Platform-conditional CA cert loader. Untested, but used in production HTTP path |
| `services/protocols.py` | 436 | 🟢 None | Protocol definitions only (typing contracts). No runtime logic to test |
| `vdf/vdict.py` | 221 | 🟡 Medium | Vendored third-party Valve Data Format parser. No test coverage |

### Assessment

The uncovered modules total **710 lines**, but of those:
- **436 lines** are protocol definitions (not testable)
- **35 lines** are trivial (enum + base class)
- **239 lines** deserve coverage (`certifi_bundle.py` + `vdf/vdict.py`)

**Effective gap: ~239 lines (1.9% of production code)** — minimal.

---

## 6. Test Methodology Analysis

### 6.1 Mocking Strategy

**Primary: `unittest.mock` (MagicMock + AsyncMock + patch)**

Every service test creates a fixture that instantiates the real service under test with mocked dependencies. Pattern:

```python
@pytest.fixture
def service(tmp_path):
    romm_api = MagicMock()
    steam_config = MagicMock()
    # ... configure returns/side_effects
    return DownloadService(romm_api=romm_api, steam_config=steam_config, ...)
```

`AsyncMock` is used for `decky.emit` and async API methods. `patch` is used sparingly for module-level function replacement.

**Secondary: Custom fakes (`FakeSaveApi`)**

`tests/fakes/fake_save_api.py` (302 lines) is an in-memory implementation of the full RomM save API contract:

- Stores saves, ROMs, notes in plain dicts
- Logs every call to a `calls` list for assertion
- Supports `inject_error()` for fault injection
- Has realistic file I/O (actual save upload/download)
- Implements upsert logic matching real server behavior

This is used by `test_saves.py` (187 tests) and `test_plugin_saves.py` (43 tests), providing **much higher fidelity** than MagicMock for the complex save sync engine.

**Observation:** The `FakeSaveApi` pattern is excellent and could be replicated for other complex adapters (e.g., a `FakeRommApi` for library sync testing, or a `FakeSteamConfig` for shortcut testing).

### 6.2 Test Types

| Type | Count | Notes |
|---|---|---|
| Unit tests | 1,721 | 100% of suite |
| Integration tests | 0 | No tests hit real services or combine real components |
| End-to-end tests | 0 | No tests exercise the full plugin lifecycle |
| Contract/API freeze tests | 1 | `test_api_freeze.py` — verifies API interface stability |
| Property-based tests | 0 | No use of Hypothesis or similar |
| Snapshot tests | 0 | No golden file / snapshot testing |

### 6.3 Parameterized Testing

**Underutilized.** Only **2 uses** of `@pytest.mark.parametrize` in the entire suite, both in `tests/lib/test_errors.py`:

```python
@pytest.mark.parametrize("exc_class", [AuthenticationError, ServerError, ...])
def test_all_subclasses_catchable(self, exc_class):
    ...
```

Many test files have repetitive patterns that would benefit from parameterization:

- `test_save_extensions.py`: 12 separate tests for different platform extension maps → could be 1 parameterized test
- `test_emulator_tag.py`: 12 tests for different emulator string patterns → could be 1 parameterized test
- `test_steam_categories.py`: 9 tests for genre → category mapping → could be 1 parameterized test
- `test_save_status.py`: 11 tests for status formatting → could be fewer with parameterization
- `test_errors.py`: Already uses it — could be the model for others

**Impact:** Not a coverage gap, but a maintainability concern. Parameterized tests are easier to extend when adding new platforms or edge cases.

### 6.4 Error & Edge Case Coverage

**Strong across the board.** Examples:

| Test File | Error/Edge Cases Covered |
|---|---|
| `test_http.py` (111 tests) | SSL errors, auth failures, timeouts, 404/409/500/502/503, connection reset, URL encoding edge cases, Unicode paths |
| `test_saves.py` (187 tests) | Clock skew clamping, duration capped to 24h, missing server IDs, both-changed conflicts, empty states, permission errors |
| `test_persistence.py` (34 tests) | Crash during write (atomic safety), missing files, corrupted JSON, empty collections, permission fixing |
| `test_downloads.py` (82 tests) | Partial downloads, cancelled downloads, disk full simulation, concurrent queue management |
| `test_firmware.py` (69 tests) | Missing BIOS files, wrong checksums, platform-specific validation, network failures |
| `test_save_conflicts.py` (32 tests) | Both-changed, neither-changed, timestamp tolerance, invalid server dates, null hashes |

### 6.5 Async Test Handling

Properly implemented:

1. `pytest.ini` sets `asyncio_mode = auto` — all `async def test_*` collected automatically
2. Many files also use explicit `@pytest.mark.asyncio` (redundant but harmless)
3. An `_set_event_loop` autouse fixture ensures `plugin.loop` matches the running event loop
4. The conftest autouse fixture resets module-level state per test

### 6.6 Test Organization

Tests are well-organized into **classes** grouping related behavior:

```python
class TestSaveConflictDetection:
    def test_both_changed_raises_conflict(self): ...
    def test_neither_changed_skips(self): ...
    def test_server_newer_downloads(self): ...

class TestSaveUpload:
    async def test_upload_new_save(self): ...
    async def test_upload_overwrites_older(self): ...
```

This provides clear grouping and discoverability. Each class typically corresponds to one method or feature of the system under test.

### 6.7 Fixture Design

Fixtures follow consistent patterns:

- **`tmp_path`** (built-in): Used extensively for filesystem isolation
- **Service fixtures**: Create real service with mocked dependencies
- **Plugin fixtures**: Create Plugin instance with full mock wiring
- **Autouse fixtures**: State reset (conftest), event loop alignment
- **Helper functions**: `_install_rom()`, `_create_save()`, `_server_save()` — reusable test data builders

**Observation:** Helper functions are scoped per-file rather than shared across test files. Some duplication exists (e.g., `_install_rom` appears in multiple files with slight variations). A shared `tests/helpers.py` module could reduce this.

---

## 7. Frontend Test Gap

### Current State: Zero Tests

The frontend consists of **31 TypeScript/React files** (~9,089 lines) with **zero test coverage**:

| Directory | Files | Untested Logic |
|---|---|---|
| `src/components/` | 12 components | UI rendering, user interactions, modal flows |
| `src/utils/` | 13 modules | State management, sync progress, download store, formatters |
| `src/api/` | 1 module | Backend API call layer |
| `src/patches/` | 2 modules | Monkey-patches for Decky/Steam UI |

The CI `build` job only checks that TypeScript **compiles** (`pnpm build`), not that anything works correctly. SonarCloud explicitly **excludes `src/**` from coverage**.

### Why This Matters

The frontend contains significant logic:
- **`syncManager.ts`**: Orchestrates the sync flow from the UI side
- **`downloadStore.ts`**: Manages download queue state
- **`syncProgress.ts`**: Progress calculation and display logic
- **`connectionState.ts`**: Connection status management
- **`formatters.ts`**: Display formatting (could have bugs with edge values)
- **`launchInterceptor.ts`**: Intercepts game launches for custom behavior

### What's Feasible

Decky plugins run in a restricted browser environment, making full component testing difficult. However:

- **Utility module tests** (formatters, progress calculation, state stores) can run in Node.js with vitest/jest
- **API type contracts** can be tested without Decky
- **Component snapshot tests** may not be feasible without the Decky SDK environment

---

## 8. Cross-Platform Portability

### Current State: Linux-Only

Several production modules use Linux-only APIs:

| Module | Linux-Only Import | Impact |
|---|---|---|
| `adapters/persistence.py` | `import fcntl` | File locking — **blocks all test collection on Windows** |
| `services/saves.py` | `import fcntl` | Save file locking |
| `domain/es_de_config.py` | Linux-specific paths | Path assumptions |

**Effect:** 15 of 52 test files fail to import on Windows, preventing local test development on non-Linux machines. The CI runs on Ubuntu so this doesn't affect the pipeline, but it hinders contributor workflow.

### Potential Mitigation

- Conditional `fcntl` import with a Windows no-op fallback (for test environments only)
- Or: document that tests require Linux/WSL and add a check to conftest.py

---

## 9. Improvement Recommendations

### 9.1 Add Frontend Unit Tests (High Impact)

**What:** Add vitest (or jest) for the 13 utility modules in `src/utils/`.

**Why:** 9,089 lines of TypeScript with zero tests. The utility modules contain pure logic (formatters, progress calculation, state management) that can be tested in Node.js without Decky.

**Scope:**
- `formatters.ts` — display formatting edge cases
- `syncProgress.ts` — progress bar calculations
- `downloadStore.ts` — queue state transitions
- `connectionState.ts` — connection status transitions
- `collections.ts` — collection management logic

**Estimated effort:** 2-3 days for initial coverage of utility modules.

### 9.2 Expand the Fake Pattern (Medium Impact)

**What:** Create `FakeRommApi` (similar to `FakeSaveApi`) for library sync testing.

**Why:** `test_library.py` (183 tests) currently uses MagicMock for the RomM API, which:
- Doesn't validate call signatures
- Can't easily simulate multi-step API interactions
- Requires tedious `side_effect` chains for complex flows

A `FakeRommApi` with in-memory platform/ROM storage would make library sync tests more readable and realistic.

**Estimated effort:** 1-2 days.

### 9.3 Add Parameterized Tests Where Repetitive (Low Impact, High Maintainability)

**What:** Convert repetitive per-case test methods to `@pytest.mark.parametrize`.

**Candidates (highest ROI):**
- `test_save_extensions.py` (12 tests → ~2 parameterized)
- `test_emulator_tag.py` (12 tests → ~2 parameterized)
- `test_steam_categories.py` (9 tests → ~1 parameterized)
- `test_save_status.py` (11 tests → ~3 parameterized)

**Why:** Easier to add new platforms/extensions — just add a tuple to the parameter list instead of a new test method.

**Estimated effort:** Half a day.

### 9.4 Test `certifi_bundle.py` (Low Impact)

**What:** Add 3-4 tests for the CA certificate bundle loader.

**Why:** Used in the production HTTP path for SSL verification. Currently untested. The module is only 18 lines but handles platform-conditional paths.

**Estimated effort:** 1 hour.

### 9.5 Add Integration Smoke Tests (Medium Impact, High Effort)

**What:** Create a small integration test suite that exercises the full bootstrap → service wiring → mock API interaction path.

**Why:** Currently there's no test that validates the entire wiring works together. `test_bootstrap.py` verifies types are correct, but doesn't exercise a real sync flow through the wired services.

**Example:** Bootstrap with a fake HTTP server (httpx mock or aiohttp test server), trigger a library sync, verify the correct files are written to disk.

**Estimated effort:** 3-5 days.

### 9.6 Add Property-Based Testing for Parsers (Low Impact, High Confidence)

**What:** Use Hypothesis for:
- URL encoding in `romm/http.py`
- VDF parsing in `vdf/vdict.py`
- Save path resolution in `domain/save_path.py`
- Filename stem matching in `domain/rom_files.py`

**Why:** Parser and encoder code is exactly where property-based testing shines — it can find edge cases that manually-written tests miss (Unicode, empty strings, paths with special characters).

**Estimated effort:** 2-3 days.

### 9.7 Shared Test Helpers Module (Low Impact, Maintainability)

**What:** Extract common helper functions (`_install_rom`, `_create_save`, `_server_save`, `_make_retry`) into a `tests/helpers.py` module.

**Why:** These helpers are duplicated with slight variations across `test_plugin.py`, `test_plugin_saves.py`, `test_saves.py`, `test_downloads.py`, and `test_library.py`. A single source of truth reduces maintenance when save/ROM data structures change.

**Estimated effort:** Half a day.

### 9.8 Cross-Platform Test Collection (Low Impact)

**What:** Add conditional `fcntl` handling so tests can at least be **collected** on Windows (even if some are skipped).

**Why:** Contributors on Windows/macOS can't run any tests locally. Even marking Linux-only tests with `@pytest.mark.skipif(sys.platform == 'win32')` would be better than failing to import.

**Estimated effort:** Half a day.

---

## 10. Priority Matrix

| # | Recommendation | Impact | Effort | Priority |
|---|---|---|---|---|
| **9.1** | Frontend unit tests (utils) | 🔴 High | 2-3 days | **P1** — 9K lines with zero tests |
| **9.2** | FakeRommApi for library tests | 🟡 Medium | 1-2 days | **P2** — improves largest test file |
| **9.5** | Integration smoke tests | 🟡 Medium | 3-5 days | **P2** — validates wiring |
| **9.3** | Parameterized testing | 🟢 Low | 0.5 day | **P3** — maintainability |
| **9.6** | Property-based testing | 🟢 Low | 2-3 days | **P3** — confidence |
| **9.7** | Shared test helpers | 🟢 Low | 0.5 day | **P3** — maintainability |
| **9.4** | Test certifi_bundle.py | 🟢 Low | 1 hour | **P4** — minimal gap |
| **9.8** | Cross-platform collection | 🟢 Low | 0.5 day | **P4** — contributor DX |

---

## Summary Statistics

| Metric | Value |
|---|---|
| Python source files | 49 |
| Python source lines | ~12,357 |
| TypeScript source files | 31 |
| TypeScript source lines | ~9,089 |
| Test files | 52 |
| Test functions | **1,721** |
| Test lines | ~26,704 |
| Python test-to-source ratio | **2.16:1** |
| Source modules with no test | 5 (710 lines, 436 are protocol defs) |
| Effective untested Python | ~239 lines (1.9%) |
| Frontend test coverage | **0%** |
| Parameterized tests | 2 (underutilized) |
| Integration tests | 0 |
| Custom fakes | 1 (FakeSaveApi — excellent pattern) |
| CI quality gates | 4 (test, build, lint, SonarCloud) |
| Architecture contracts | 6 (import-linter) |

### Verdict

The Python test suite is **production-grade** — high coverage, strong error/edge case testing, clean architecture enforcement, and a well-designed fake pattern. The primary improvement opportunities are: (1) bringing the frontend under test, (2) expanding the fake pattern to more adapters, and (3) adding integration smoke tests to validate end-to-end wiring.
