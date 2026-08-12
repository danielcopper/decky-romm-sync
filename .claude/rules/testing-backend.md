---
paths:
  - "tests/**"
---

# Backend testing

Every backend feature or callable where testing makes sense MUST have unit tests: **happy path**, **bad path** (invalid
input, missing data, API errors, network failures), and **edge cases** (empty strings, None, masked values `"••••"`,
boundaries). Tests mirror the source structure (`tests/services/`, `tests/adapters/`, …), one test file per source
module. Shared mocks live in `tests/conftest.py`.

## Property-based tests — pure decision kernels (hypothesis)

The pure decision kernels carry a property tier on top of hand-enumerated cases. The in-tree ones
(`domain/save_path.py`, `domain/iso_time.py`) have theirs in `tests/domain/test_*_property.py`; the save-sync decision
is made by the compiled gavel core, so its properties drive `GavelNativeAdapter` — the production seam — from
`tests/adapters/test_gavel_native_property.py`. Properties state the safety invariant directly (no destructive action
without a recovery source; decisions stable under timestamp-format variation; replay determinism). `hypothesis` is
dev-only and never ships.

**A property earns its place only where a vector cannot reach.** It either quantifies over a space the vendored vectors
do not exhaust, or it relates several runs to each other — the same decision under two ISO renderings, a replay, a state
sequence — which one input bound to one output can never say. A point statement about a single row of the decision table
belongs in a vector; stating it as a property too is two places to maintain for one rule.

**A property states the TRUE invariant, never a watered-down one.** If the invariant's fix is still open, the property
FAILS today — pin it `@pytest.mark.xfail(strict=True, reason="#<issue>: <one-line>")`. When the fix lands the property
passes → XPASS → CI fails → the marker must be removed. A property is never weakened to go green.

## Contract tests — real `Plugin` over real `bootstrap`

`tests/contract/` crosses the frontend↔backend wire: it builds the **real** `Plugin` through the **real** `bootstrap()`
and `wire_services()` (real settings dict, real SQLite + migrations, real file-store adapters, all under `tmp_path`) and
drives the actual `main.py` callables. Only the outermost edges are faked (`romm_api`, `sgdb_adapter`,
Clock/UuidGen/Sleeper, `emit`, and `http_adapter.with_retry` as a single-attempt pass-through). Harness lives in
`tests/contract/_harness.py`; seeding helpers in `tests/contract/_seed.py`.

- **Call callables exactly as the frontend does** — positional, JSON-shaped arguments with the arg types declared in
  `src/api/backend.ts` (literal `None` where the TS type says `null`).
- **Assert the response SHAPE + behavior, not delegation.** Pin the literal dict keys, the canonical failure shape, the
  discriminated-status union, and the partial-success carve-outs. Where a callable has a server-reachable failure mode,
  exercise BOTH the happy path AND the failure path.
- The `harness` fixture is **async** so it binds the test's running event loop. Each test gets a fresh `tmp_path`.

`scripts/check_callable_manifest.py` derives the frontend surface from every `callable<[Args], Return>("name")` in
`src/**/*.ts` and the backend surface from the public `async def` methods on `Plugin`, failing on any name or arity
divergence. It runs standalone in CI and inside pytest via `tests/contract/test_callable_manifest.py`.

## Gate tests — `tests/scripts/`

Every `scripts/check_*` gate carries its own test file. The gate is loaded via `importlib` (`scripts/` is not on
`sys.path`), its module constants are monkeypatched at a `tmp_path` tree, and the real walk runs against that layout. A
gate test that only asserts "passes on the real repo" is vacuous — pin each failure mode the gate is supposed to catch,
and its boundaries.

## Gavel conformance vectors — vendored contract tier

Two [romm-gavel](https://github.com/danielcopper/romm-gavel) vector families guard the save-sync decisions: **ladder**
(the 409 resolution, `tests/adapters/test_gavel_native.py`) and **decision-table** (the full per-file decision,
`tests/adapters/test_gavel_native_table_vectors.py`). Both decisions run in the compiled core
(`adapters/gavel_native.py`) and nowhere else, so each family runs against that core through `GavelNativeAdapter` — the
production path — and there is no second implementation to hold to the same contract. The vectors are vendored verbatim
under `tests/adapters/gavel_vectors/` at a pinned upstream **release tag** (no submodule, no network in CI), so a
contract change lands as a reviewable diff. **Never edit a vector to make the core pass** — updating means deliberately
re-copying the JSON and bumping the tag in that folder's `README.md`, in lockstep with the vendored `.so`.

**What these vectors test here is our marshalling, not gavel's decisions.** Upstream tells a consumer of the compiled
core not to run them — the core satisfies them by construction and upstream CI proves it on every change, so running
them again tests upstream. That reading is right about the decisions and wrong about the path they travel: a vector
replayed here goes through `GavelNativeAdapter` first, which packs a RomM payload into C structs, turns an ISO timestamp
into an epoch plus a known-flag, keeps "no size recorded" apart from "zero bytes", and resolves the answer back to the
caller's own save dict. That layer is ours, and it is where a bug of ours would sit. The vectors are worth their keep
because they hand us a large set of input/expected pairs we did not have to invent — and a decision we invent ourselves
only ever confirms our own reading of the contract.

They earn it in practice, not just in principle: deliberately breaking the marshalling (reading a 0-byte size as "no
size", swapping the two recorded hashes, taking `is_current` from key presence rather than value, dropping the
adopt-baseline flag) turns this tier red, and for one of those breakages it is the only tier that notices.

## emu-atlas conformance vectors — vendored contract tier

The same self-conformance pattern proves the plugin's save-path kernel agrees with
[emu-atlas](https://github.com/danielcopper/emu-atlas), the config-aware emulator-knowledge library extracted from this
plugin, where the two overlap. Its `machines` vector family (16 fixture machines in, detected installations + save
placements out) runs against the real adapters (`RetroDeckPathsAdapter` + `RetroArchConfigAdapter`) and domain functions
(`resolve_save_dir` / `compute_local_save_target`) in `tests/test_atlas_machine_vectors.py`. The overlap is partial by
design — the plugin has no standalone-RetroArch saves-root concept (its saves base always comes from RetroDECK paths) —
so every vector carries an explicit `_CHECK_LEVELS` entry: `full` (end-to-end dir + filename), `layout-only` (only the
`retroarch.cfg` interpretation — sort flags or the `ContentDir` classification), or `n/a` (no overlap). No vector is
silently skipped: a new upstream vector without an allowlist entry fails at collection. Vectors are vendored verbatim
under `tests/atlas_vectors/machines/` at a pinned upstream release tag; **never edit a vector to make the kernel pass**
— updating means re-copying the JSON and bumping the tag in that folder's `README.md`.
