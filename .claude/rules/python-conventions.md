---
paths:
  - "py_modules/**/*.py"
  - "main.py"
---

# Python conventions — naming, docstrings, layout

None of the rules below has a mechanical check. They hold only if they are carried at the point of writing.

## Protocol naming — suffix by shape `[ours]`

- `…Reader` — object-shaped Protocols with multiple methods (`RetroArchConfigReader`).
- `…Provider` / `…Fn` — call-shaped (`__call__`-only) Protocols (`RetroArchSaveSortingProvider`, `CoreNameProviderFn`).
- `…Store` — file-store Protocols (`CoverArtFileStore`).
- `…Cache` — cache Protocols (`SgdbArtworkCache`).
- `…Persister` — persistence Protocols (`SettingsPersister`).
- Bare names — pervasive cross-cutting primitives (`Clock`, `Sleeper`, `UuidGen`, `DebugLogger`).

A sibling set that mixes suffixes reflects a shape difference, not an inconsistency.

## Async/sync method naming `[ours]`

- Async methods carry the bare domain-verb name — no `_async` / `Async` suffix.
- A **synchronous twin** (typically a lock-free worker run via `run_in_executor` that a peer calls directly to avoid
  re-entering a lock) is named `do_<verb>` if public/peer-called, `_<verb>_io` if private/internal-only. The async
  public method keeps the bare verb.

The two idioms coexist by access level; converging them is open work, so match the idiom already used in the file you
are editing rather than introducing the other one.

## Docstrings — intent over behavior

**Module and class docstrings** describe **what belongs here** (the contract), not what is currently in the file
(behavior). Behavior listings rot when methods change; contracts don't.

- Bad (class): `"""Owns save_sync_state.json — persistence, migrations, default construction."""` (rots when a 4th
  responsibility lands)
- Good (class): `"""Owns save_sync_state.json — single source of truth for on-disk save-sync state."""`

**Method docstrings are different** — one method's contract is naturally scoped, so describing behavior, parameters, and
return value is fine and stays in sync with the signature.

Avoid: "mechanical extraction from X", "during the transition", "moved from Y", "added for the Z flow", "see PR #123" —
commit-message content that rots in source.

## Subfolder layout `[ours]`

Layer top-level folders are flat by default — one file per concept. A subfolder is justified **only when the modules
within share an internal type, helper, or state**, not when they share a brand-name prefix. `adapters/romm/` qualifies
(`http.py` is the internal transport for the public `romm_api.py`); `adapters/retroarch/` would not (a config reader and
a core lookup share only a brand name). Service decompositions with shared state qualify — `services/saves/`,
`services/library/`.

## Sub-package `__init__.py` `[ours]`

- **Top-level layer namespace** (`adapters/`, `services/`, `domain/`, `lib/`, `models/`): empty (docstring optional).
  Consumers deep-import.
- **Consumed via package import** (`from package import X`): contract-style module docstring, re-exports of the public
  class(es), optional `__all__`. Examples: `services/saves/`, `services/saves/sync_engine/`.
- **Consumed only via deep-import**: empty or docstring, no re-exports. Example: `adapters/romm/`.

Implementation never lives in `__init__.py` — it is a namespace marker plus re-exports.
