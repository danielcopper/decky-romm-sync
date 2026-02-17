# Backend Architecture

## Overview

The Python lib (`main.py` + `lib/`) uses a **mixin-based** architecture. A single `Plugin` class inherits from 8 mixin classes, each owning a distinct domain. At runtime, Python's MRO (Method Resolution Order) composes them into one object — all methods are accessible on `self`, and Decky's `callable()` discovery works transparently.

```python
class Plugin(StateMixin, RommClientMixin, SgdbMixin, SteamConfigMixin,
             FirmwareMixin, MetadataMixin, DownloadMixin, SyncMixin)
```

## Module Responsibilities

| Module            | Lines | Domain                                                       |
| ----------------- | ----- | ------------------------------------------------------------ |
| `state.py`        | ~100  | Settings, state, metadata cache persistence                  |
| `romm_client.py`  | ~80   | RomM HTTP client, platform config loading                    |
| `steam_config.py` | ~180  | VDF operations, Steam paths, Steam Input config              |
| `firmware.py`     | ~215  | BIOS/firmware status, download, platform checks              |
| `metadata.py`     | ~95   | ROM metadata extraction, caching (7-day TTL)                 |
| `sgdb.py`         | ~225  | SteamGridDB artwork fetch, API key management                |
| `downloads.py`    | ~395  | ROM downloads, multi-file/ZIP, M3U, cleanup                  |
| `sync.py`         | ~710  | Sync engine, registry, artwork, platform management          |
| `main.py`         | ~115  | Plugin class composition, lifecycle, thin settings callables |

## Runtime Dependency Diagram

Each mixin documents its runtime dependencies via a `TYPE_CHECKING` Protocol. These are the `self.*` attributes and methods each mixin calls at runtime — provided by other mixins through the shared `Plugin` instance.

```txt
                    ┌────────────┐
                    │   state    │  (no dependencies)
                    └────────────┘
                          ▲
              ┌───────────┼───────────────────────┐
              │           │                       │
        ┌─────────┐  ┌─────────────┐        ┌──────────┐
        │  romm   │  │   steam     │        │ metadata │
        │ client  │  │   config    │        │          │
        └─────────┘  └─────────────┘        └──────────┘
              ▲           ▲                  ▲    ▲
              │           │                  │    │
     ┌────────┼───────────┼──────────────────┘    │
     │        │           │                       │
     │   ┌────┴────┐  ┌───┴──┐  ┌──────────┐      │
     │   │firmware │  │ sgdb │  │downloads │      │
     │   └─────────┘  └──────┘  └──────────┘      │
     │        ▲           ▲          ▲            │
     │        │           │          │            │
     │        └───────────┼──────────┘            │
     │                    │                       │
     │              ┌─────┴────┐                  │
     └──────────────┤   sync   ├──────────────────┘
                    └──────────┘
```

Arrow direction: depends-on (A → B means A calls methods from B).

### Dependency Details by Module

**state** — no runtime deps on other mixins

```python
Provides: settings, _state, _metadata_cache,
          _load_settings(), _save_settings_to_disk(), _log_debug(),
          _load_state(), _save_state(), _prune_stale_state(),
          _load_metadata_cache(), _save_metadata_cache()
```

**romm_client** — depends on: state

```python
Uses: self.settings (from state)
Provides: _romm_request(), _romm_download(), _resolve_system(), _load_platform_map()
```

**steam_config** — depends on: state

```python
Uses: self.settings, self._state (from state)
Provides: _find_steam_user_dir(), _grid_dir(), _shortcuts_vdf_path(),
          _generate_app_id(), _generate_artwork_id(),
          _read_shortcuts(), _write_shortcuts(),
          _set_steam_input_config(), _check_retroarch_input_driver(),
          apply_steam_input_setting()
```

**firmware** — depends on: state, romm_client

```python
Uses: self._state, self.loop (from state/Plugin)
      self._romm_request(), self._romm_download() (from romm_client)
```

**metadata** — depends on: state, romm_client

```python
Uses: self._metadata_cache, self._state, self.loop (from state/Plugin)
      self._log_debug() (from state)
      self._romm_request() (from romm_client)
      self._save_metadata_cache() (from state)
```

**sgdb** — depends on: state, romm_client

```python
Uses: self.settings, self._state, self._pending_sync, self.loop (from state/Plugin)
      self._save_settings_to_disk(), self._log_debug() (from state)
      self._romm_request() (from romm_client)
      self._save_state() (from state)
```

**downloads** — depends on: state, romm_client

```python
Uses: self._download_in_progress, self._download_queue, self._download_tasks,
      self._state, self.loop (from state/Plugin)
      self._romm_request(), self._romm_download() (from romm_client)
      self._resolve_system() (from romm_client)
      self._save_state() (from state)
```

**sync** — depends on: state, romm_client, steam_config, metadata

```python
Uses: self.settings, self._state, self._sync_running, self._sync_cancel,
      self._sync_progress, self._pending_sync, self._metadata_cache, self.loop
      self._romm_request(), self._romm_download() (from romm_client)
      self._save_settings_to_disk(), self._save_state(), self._save_metadata_cache(),
      self._log_debug() (from state)
      self._extract_metadata() (from metadata)
      self._grid_dir(), _set_steam_input_config(), _generate_app_id(),
      _generate_artwork_id(), _read_shortcuts(), _write_shortcuts() (from steam_config)
```

## Boundary Enforcement

### 1. import-linter (compile-time)

The `.importlinter` config enforces an **independence contract**: no mixin module may import from any other mixin module at the file level. This prevents accidental tight coupling.

```ini
[importlinter:contract:mixin-independence]
name = Mixins must not import each other
type = independence
modules =
    lib.state
    lib.romm_client
    ...
```

Run with `lint-imports` (or `mise run lint`). CI should gate on this.

### 2. TYPE_CHECKING Protocols (documentation)

Each mixin declares a `_*Deps` Protocol inside `if TYPE_CHECKING:` that documents exactly which `self.*` attributes and methods it expects at runtime. These have zero runtime cost — they exist purely as documentation and for IDE support.

```python
if TYPE_CHECKING:
    class _MetadataDeps(Protocol):
        _metadata_cache: dict
        _state: dict
        loop: asyncio.AbstractEventLoop
        def _log_debug(self, msg: str) -> None: ...
        def _romm_request(self, path: str) -> Any: ...
        def _save_metadata_cache(self) -> None: ...
```

### 3. Convention: underscore prefix

All internal/cross-mixin methods use `_` prefix. Public callables (exposed to frontend via `callable()`) have no prefix. This makes it clear which methods are API surface vs internal wiring.

## When to Reconsider

The mixin pattern works well at the current scale (~2000 lines across 8 modules, single maintainer). Consider switching to **delegation** (separate service objects injected into Plugin) if:

- The codebase exceeds ~4000 lines of lib logic
- Multiple contributors need to work on modules simultaneously
- Circular runtime dependencies emerge between mixins
- Testing requires isolating one mixin from another's side effects
