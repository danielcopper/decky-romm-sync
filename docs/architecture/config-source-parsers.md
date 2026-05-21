# Config Source Parsers

## Overview

The plugin reads configuration and metadata from multiple local files — ES-DE XML, RetroArch `.info` files, `retrodeck.json`, `retroarch.cfg`, gamelist XML, a bundled `core_defaults.json`, and more. Each source has its own format, its own update cycle, and its own authoritative domain. None of them is redundant with another, even when they describe overlapping concepts like "core name" or "supported extensions".

This page catalogs the sources, the rules for parsing them, and the mapping of questions to sources. It is the reference that new parsers should follow and the justification for why existing parsers are shaped the way they are.

**Not covered on this page:** HTTP API clients (RomM, SteamGridDB, RetroAchievements). Those are handled as network-facing adapter clients, not as config-source parsers. The same layering principles apply but the mechanics (auth, retries, rate limiting) are different enough that they belong in their own documents.

## Why multiple sources?

RetroDECK is a stack of independent components — ES-DE as the frontend, RetroArch as the runtime, a glue config layer on top, plus bundled tooling. Each component owns its own metadata in its own format:

- **ES-DE** keeps its system definitions in `es_systems.xml`, its per-game overrides in `gamelist.xml`, and display-oriented labels it chose to include. It decides which core runs a ROM.
- **RetroArch** keeps per-core metadata in `.info` files shipped alongside each `.so` — `corename`, `display_name`, `supported_extensions`, `firmware_*`, `database`, and more. It decides how saves are organized on disk, which firmware to require, and what extensions each core accepts.
- **RetroDECK glue** keeps user-facing configuration in `retrodeck.json` (paths for ROMs, saves, BIOS, state) and forwards RetroArch's own `retroarch.cfg` (sort settings, core directories, and everything else RetroArch reads at startup).

There is no unified source. The upstream components are developed independently, updated on independent schedules, and have no mechanism to consolidate their metadata. The plugin must read each source individually.

Attempting to "normalize" these into a single internal model would fight against upstream — every time ES-DE renames a label or RetroArch ships a new core, the normalization table would drift. The sustainable approach is the opposite: keep each source as its own parser, keep the parsers ignorant of each other, and let services choose the right parser per flow.

## Principle: one parser per source, no cross-contamination

Each external source gets exactly one parser. Parsers do not fall back to each other. Services are responsible for picking the right parser for each business flow.

The principle has three parts:

1. **One parser per source.** Adding a second reader for the same file creates drift — sooner or later the two readers disagree and the bug lives in the newer one. `retrodeck.json` has one parser. `es_systems.xml` has one parser. Every new source follows suit.

2. **No cross-contamination.** When a parser cannot answer a question — the file is missing, a field is absent, the value is malformed — the parser returns `None` (or raises), it does **not** defer to another parser. Cross-parser fallbacks are how ES-DE's display label ended up being used as a RetroArch save directory name (the underlying bug of [#208](https://github.com/danielcopper/decky-romm-sync/issues/208)). Each source owns its own answer or admits it has none.

3. **Services choose the parser per flow.** "Which core is active for this ROM?" is an ES-DE question. "What does RetroArch call that core in its own subsystem?" is a RetroArch question. Both questions may appear inside the same service method, and the service is responsible for asking each one at the right parser. Do not invent a parser-of-parsers to hide the choice.

### Worked example: ES-DE label vs RetroArch corename

The concrete case that motivated this principle, and the bug in [#208](https://github.com/danielcopper/decky-romm-sync/issues/208):

- `CoreResolver` (parses ES-DE) returns a tuple `(core_so, label)` for "which core is active?". `label` is ES-DE's **display string** — e.g. `"Snes9x - Current"`. It is a UI-level name, chosen by the ES-DE/RetroDECK team to disambiguate in the core picker UI.
- RetroArch, when `sort_savefiles_enable = true`, writes saves into subdirectories named by the **`corename`** field of the core's `.info` file — e.g. `"Snes9x"`. It is RetroArch's canonical internal name, set by the core's maintainer, baked into RetroArch's runtime path logic.

These two values **are not redundant representations of the same thing**. They answer different questions at different layers:

| Core | ES-DE label | RetroArch `corename` |
| --- | --- | --- |
| Snes9x | `Snes9x - Current` | `Snes9x` |
| mGBA | `mGBA` | `mGBA` |
| Beetle PSX HW | `Beetle PSX HW` | `Beetle PSX HW` |
| SwanStation | `SwanStation` | `SwanStation` |
| Genesis Plus GX | `Genesis Plus GX` | `Genesis Plus GX` |

Four out of five happen to match textually. Snes9x does not — ES-DE added `" - Current"` to disambiguate from the older `Snes9x 2010` variant. The match is **incidental**, not structural. Future cores, future ES-DE redesigns, and future RetroDECK re-labelings will introduce new mismatches.

Reconciling by whitelist — a table of "ES-DE label → RetroArch corename" mappings — would be a perpetual maintenance burden. Every new core, every label change, every RetroDECK release shifts the table. The correct answer is to not reconcile at all: when you need the save directory name, ask RetroArch; when you need the UI label, ask ES-DE. The lookup is O(1) per source, caching is local, and drift is impossible because neither parser pretends to speak for the other.

## Question-to-source mapping

| Question | Authoritative source | Why |
| --- | --- | --- |
| Which core is active for system X / ROM Y? | ES-DE (`es_systems.xml` + `gamelist.xml`) | ES-DE **decides** this — defaults, per-system overrides, per-game overrides. |
| What's the ES-DE display label for a core? | ES-DE | Label is an ES-DE/RetroDECK UI concern, chosen at the ES-DE config level. |
| What subdirectory does RetroArch use for this core's saves (sort-by-core)? | RetroArch `.info` `corename` field | RetroArch creates the directory using `corename`; `.info` is the only place that canonical name lives. |
| What ROM extensions does a core support? | RetroArch `.info` `supported_extensions` field | libretro-maintainer-authoritative, updated with every core release. |
| What firmware files does a core need? | RetroArch `.info` `firmware_count` + `firmwareN_*` fields | libretro-maintainer-authoritative; optional flags included. |
| What datfile database matches a core's ROMs? | RetroArch `.info` `database` field | libretro-maintainer-authoritative. |
| Where does RetroDECK put ROMs, saves, BIOS, states, and its home directory? | `retrodeck.json` | RetroDECK owns path configuration; it's the file users edit via the RetroDECK configurator. |
| Is RetroArch's save-sorting by content or by core enabled? | `retroarch.cfg` `sort_savefiles_*` | RetroArch owns its runtime config; the cfg is its canonical input. |

If a new question appears, the first step is to figure out which source authoritatively owns it. The mapping above grows as new questions are added — treat this table as part of the contract, not a passive catalog.

## Parser layout template

New parsers follow a strict domain-plus-adapter split, matching the broader service/adapter architecture documented in [Backend Architecture](backend-architecture.md): a pure parse function in `domain/`, all I/O in an `adapters/` class, and a callback Protocol that services depend on. The `.info` parser below (`domain/retroarch_core_info.py` + `adapters/retroarch_core_info.py`) is the reference shape.

```text
┌──────────────────────────────────────┐
│ services/                            │
│   <SomeService> depends on callback  │
└────────────┬─────────────────────────┘
             │ uses
┌────────────▼─────────────────────────┐
│ services/protocols/ (paths.py)       │
│   <Capability>ProviderFn (Protocol)  │
└────────────▲─────────────────────────┘
             │ implemented by method of
┌────────────┴─────────────────────────┐
│ adapters/<source>.py                 │
│   <Source>Adapter (I/O + caching)    │
└────────────┬─────────────────────────┘
             │ delegates parsing to
┌────────────▼─────────────────────────┐
│ domain/<source>.py                   │
│   pure parse function(s)             │
└──────────────────────────────────────┘
```

### 1. Pure parser in `py_modules/domain/<source>.py`

A module of pure functions. No I/O, no logging, no filesystem access. Takes text (or already-loaded data) as input and returns a structured representation.

```python
# domain/retroarch_core_info.py

def parse_core_info(text: str) -> dict[str, str]:
    """Parse a RetroArch .info file's content into a key-value dict.

    Format: INI-like. Lines of the form `key = "value"`, `#` comments,
    blank lines. All values returned as strings — the caller decides
    how to interpret each field.
    """
    ...
```

Why in domain: parsing is pure logic. It's exhaustively testable with inline text fixtures — no tmp_path, no mocks, no subprocess. This is the cleanest possible unit under test, and it's where format-level edge cases belong (comments, blank lines, unquoted values, escaped characters, line-continuation quirks, etc.).

### 2. I/O-owning adapter in `py_modules/adapters/<source>.py`

A class with a single responsibility: resolve the right file path(s), read bytes, delegate parsing to the domain module, cache the result, handle I/O errors.

```python
# adapters/retroarch_core_info.py

class RetroArchCoreInfoAdapter:
    _SYSTEM_CORES_DIR = "/var/lib/flatpak/app/net.retrodeck.retrodeck/current/active/files/..."
    _USER_CORES_SUFFIX = os.path.join(".local", "share", "flatpak", ...)

    def __init__(self, *, user_home: str, logger: logging.Logger) -> None: ...

    def get_core_info(self, core_so: str) -> dict[str, str] | None:
        """Resolve path, read file, parse. None on any failure."""

    def get_corename(self, core_so: str) -> str | None:
        """Convenience accessor for the corename field."""
```

Why in adapter: anything that touches the filesystem (open, read, stat, glob) is I/O and belongs in the adapter layer per the plugin's layering rules. Adapters are allowed to import domain modules — the reverse is not allowed (enforced by import-linter).

### 3. Protocol(s) in `py_modules/services/protocols/`

Services never import concrete adapters. They depend on callable protocols defined in the `services/protocols/` package (config-source parsers live in `paths.py`), and the concrete adapter method is wired up by `bootstrap.py`.

```python
class CoreNameProviderFn(Protocol):
    def __call__(self, core_so: str) -> str | None: ...
```

One protocol per capability, not one protocol per adapter. This matches the existing pattern of individual callback protocols (`SavesPathProvider`, `RomsPathProvider`, `BiosPathProvider`, etc.) introduced by the #196 refactor. When the same adapter exposes multiple capabilities (`get_corename`, `get_supported_extensions`, `get_firmware_requirements`, …), each gets its own protocol so services can depend on only what they actually use — and a test double only needs to stub the callables a given test exercises.

### 4. Wiring in `py_modules/bootstrap.py`

Adapter instance is created in the composition root, and its method is threaded into services that need it:

```python
retroarch_core_info = RetroArchCoreInfoAdapter(user_home=user_home, logger=logger)
core_resolver = CoreResolver(plugin_dir=plugin_dir, logger=logger, ...)

migration_service = MigrationService(
    config=MigrationServiceConfig(
        ...,
        get_active_core=core_resolver.get_active_core,      # ES-DE question
        get_core_name=retroarch_core_info.get_corename,     # RetroArch question
    ),
)
```

The service receives callbacks, not an adapter. It has no knowledge of which file or format they come from — which keeps services independent of any single parser and makes them straightforward to test with fake callables.

### 5. Tests at each layer

- **`tests/domain/test_<source>.py`** — pure-parse tests with inline string fixtures. Edge cases, malformed input, Unicode, whitespace variations, missing fields. No tmp_path, no mocks.
- **`tests/adapters/test_<source>.py`** — adapter tests with `tmp_path`. Happy path, file missing, `OSError`, cache hits, candidate-path fallback. This is the only layer that touches real files.
- **`tests/services/test_<service>.py`** — service tests with the callback mocked (`MagicMock`). Flow tests: what happens on `None`, what happens on success, what happens when the callback is not injected at all.

## Current parsers

| Source | Format | Parser location | Layer status | What it answers |
| --- | --- | --- | --- | --- |
| `retrodeck.json` | JSON | `adapters/retrodeck_paths.py` — `RetroDeckPathsAdapter` | ✅ adapter (correct layering) | Where RetroDECK puts saves, ROMs, BIOS, and its home directory. |
| `retroarch.cfg` | INI-ish `key = "value"` | `adapters/retroarch_config.py` — `RetroArchConfigAdapter` | ✅ adapter (correct layering) | Save-sorting flags (`sort_savefiles_by_content_enable`, `sort_savefiles_enable`); room to grow as more cfg fields are needed. |
| `es_systems.xml` | XML | `adapters/es_de_config.py` — `CoreResolver` | ✅ adapter (correct layering) | Which core is ES-DE's default for a system; list of available cores with labels. |
| `gamelist.xml` | XML | `adapters/es_de_config.py` — `CoreResolver` + `GamelistXmlEditorAdapter` | ✅ adapter (correct layering) | Per-system and per-game `altemulator` overrides; metadata reads and writes. |
| `core_defaults.json` (bundled) | JSON | `adapters/es_de_config.py` — `CoreResolver` | ✅ adapter (correct layering) | Static fallback for core defaults when live `es_systems.xml` is unavailable. |
| RetroArch `.info` | INI-ish `key = "value"` | `adapters/retroarch_core_info.py` + `domain/retroarch_core_info.py` | ✅ adapter + pure-domain parse (correct layering) | `corename` (authoritative save subdir name), plus the full metadata dict for future use. |

The ES-DE parsers (`CoreResolver`, `GamelistXmlEditorAdapter`) keep their XML parsing inline rather than in a separate pure-domain module; that is acceptable because the adapter owns its I/O. New `.info`-style sources that warrant a pure-parse split should follow the [parser layout template](#parser-layout-template) above (pure parse in `domain/`, I/O in `adapters/`).

## Known consumer gaps

One-parser-per-source compliance has two sides, and this page historically only covered one of them:

1. **Parser inventory (covered above).** Does each source have exactly one parser? Does that parser live in the right layer? Does it refuse to cross-contaminate?
2. **Consumer compliance (this section).** Does every service method that asks a question route it through the right parser? It is possible — and has happened in practice — for every parser to be individually correct while a consumer still builds the wrong answer by calling the wrong parser or by forgetting to call a parser at all.

The gap below is the consumer-side cautionary tale that motivated adding this section.

### Case study: SaveService missed the RetroArch corename after #208

[#208](https://github.com/danielcopper/decky-romm-sync/issues/208) introduced the `RetroArchCoreInfoAdapter` precisely to fix the "ES-DE label leaks into RetroArch save path" bug described in the [Worked example](#worked-example-es-de-label-vs-retroarch-corename) above. That PR correctly updated `MigrationService` to ask ES-DE which core is active and then ask the RetroArch `.info` parser for the canonical `corename`. The parser was written, the protocol was defined, and the migration flow was fixed.

It was not until PR #227 had already merged, during live-testing on a real Steam Deck, that the second shoe dropped: `SaveService._get_rom_save_info` — which every save-sync flow on every game launch routes through — was still calling `domain.save_path.resolve_save_dir(..., sort_by_core=True)` **without** the resolved `core_name` argument. The domain function's guard `if sort_by_core and core_name:` silently skipped the core subdir branch, so every save path fell back to `{saves_base}/{system}/` instead of `{saves_base}/{system}/{corename}/`.

The live reproduction: a user restored a Mario Golf save from the game detail page. RetroArch had `sort_savefiles_enable = "true"`, so the migrated save lived at `saves/gba/mGBA/Mario Golf - Advance Tour (USA).srm`. The restore wrote to `saves/gba/Mario Golf - Advance Tour (USA).srm` — the parent directory. RetroArch at launch read from the core subdir, so the restored save was invisible to the game.

The parsers were fine. The migration consumer was fine. The save-sync consumer was not, because during the #208 fix the author only updated the consumer that the issue explicitly named and did not audit other call sites for the same mistake. That omission is exactly what the "one parser per source" principle is supposed to prevent — but the principle had been documented only as parser-side guidance and never applied as an audit criterion against the existing codebase.

[#232](https://github.com/danielcopper/decky-romm-sync/issues/232) closed the SaveService gap and added this section to make the consumer-compliance dimension explicit.

### Consumer checklist for reviewers

When reviewing a PR that touches save-path resolution, core resolution, firmware requirements, or any other capability in the question-to-source mapping table, walk each call site with these questions:

1. **Is the right parser being called at all?** The answer to "what subdirectory does RetroArch use for this core's saves?" is always the RetroArch `.info` `corename` field. The answer to "which core runs this ROM?" is always ES-DE. A consumer that asks ES-DE for a save-directory name, or asks RetroArch which core is active, has skipped a source boundary.
2. **Are all inputs to the parser resolved from their own authoritative parsers?** `resolve_save_dir` takes `core_name` as an input — not because the domain function can compute it, but because the caller is responsible for asking the right parser first. A call that passes `core_name=None` with `sort_by_core=True` is a silent bug, because the domain guard drops the core subdir without error.
3. **Does the `None` case fail loudly?** When a required parser returns `None`, the consumer must either (a) skip the operation with a warning (MigrationService's choice — one-shot flow, acceptable to halt), or (b) log a warning and fall back to a documented behavior (SaveService's choice — continuous critical path, acceptable to degrade). **Silent fallback with no diagnostic is never acceptable** — that is what produced #232.
4. **Does the same call pattern appear elsewhere?** If you are fixing one consumer, grep the whole codebase for the same function name and audit every call site. A `resolve_retroarch_corename` helper exists on both `MigrationService` (`services/migration.py`) and the saves `RomInfoService` (`services/saves/rom_info.py`) — future save-path consumers should follow the same pattern or reuse a helper rather than inlining the two-step resolution.
5. **Is the new consumer covered by regression tests?** Every consumer should have unit tests for: the happy path, the `None` path (parser unresolvable), and the "callback not injected at all" path. The `TestGetRomSaveInfo` class in `tests/services/saves/test_rom_info.py` is the reference shape.

### Historical examples

| Issue | Parser state | Consumer state | Resolution |
| --- | --- | --- | --- |
| [#208](https://github.com/danielcopper/decky-romm-sync/issues/208) | `.info` parser did not exist; ES-DE label was the only core name source | `MigrationService` used the ES-DE label as the RetroArch save subdir name | Add `RetroArchCoreInfoAdapter`, resolve corename from `.info`, wire into migration |
| [#232](https://github.com/danielcopper/decky-romm-sync/issues/232) | `.info` parser correct (from #208) | `SaveService._get_rom_save_info` still called `resolve_save_dir` with `core_name=None`, so `sort_by_core` was a silent no-op for every save flow | Thread `get_core_name` into `SaveService`, extract `_resolve_retroarch_corename` helper mirroring `MigrationService`, warn+fallback when unresolvable |

Both issues are parser-side fine in the sense that the parser itself returned the right answer for the question asked. They are consumer-side bugs: the consumer either asked the wrong parser or forgot to ask the right one. The checklist above exists to catch the second shoe before it drops in production.

## Planned / future unlocks

The RetroArch `.info` parser, introduced by #208, ships with only the `corename` field hooked up — the minimum needed to fix the save-sort migration bug. But `.info` files contain much more, and the parser returns the full dict internally. The following unlocks are natural follow-ups; each should land in its own issue and its own PR so we can pace them against real need rather than speculatively building ahead.

| Capability | `.info` field(s) | Replaces today's | Value |
| --- | --- | --- | --- |
| Per-core supported extensions | `supported_extensions` | Hardcoded lists in `defaults/config.json` | Self-updating via Flatpak releases; fewer drift bugs when cores add formats. |
| Per-core firmware requirements | `firmware_count`, `firmware<N>_desc`, `firmware<N>_path`, `firmware<N>_opt` | Manual BIOS registry in `FirmwareService` | Authoritative list per active core; highlights what's truly needed vs optional. |
| Core switching validation | `supported_extensions` | (no check today) | Prevent assigning a core to a system whose ROM extensions it can't load. |
| DAT/database identification | `database` | (no use today) | Match ROM headers against the right datfile for integrity checks. |
| Core display names | `display_name` | ES-DE label only | Secondary source when an ES-DE label is unavailable (rare); never used to override ES-DE. |

None of these are in scope for #208. They are listed here so that, when the time comes, contributors know:

- The parser already exists — no new adapter needed.
- The right way to extend it is a new callback protocol in `services/protocols/` (the `paths.py` module) plus a new accessor method on the adapter (e.g. `get_supported_extensions(core_so)`).
- The underlying `get_core_info` already returns the full dict, so each new accessor is a few lines wrapping `info.get(field)`.
- The new accessor gets wired into bootstrap.py the same way `get_corename` is.

## How to add a new source

When the plugin needs to read a new external config/metadata source, the checklist is:

1. **Identify the source and its authoritative domain.** Add a row to the **Question-to-source mapping** table above. If the new source overlaps with an existing one, explicitly decide which parser owns which question — do not merge them.
2. **Write the pure parser** in `py_modules/domain/<source>.py`. Start with the smallest function that answers the immediate need; grow the API later. Tests first.
3. **Write the adapter** in `py_modules/adapters/<source>.py`. Path resolution, file read, parsing delegation, caching. Tests with `tmp_path`.
4. **Add callback protocol(s)** in `py_modules/services/protocols/`. One protocol per capability, in the existing `*Fn` Call-protocol style.
5. **Wire in `bootstrap.py`.** Instantiate the adapter in the composition root; thread its method(s) into services that need them.
6. **Consume in services.** Services depend on the protocol, not on the adapter. Handle the `None` / failure case at the service layer — no cross-parser fallbacks.
7. **Add a row to Current parsers** above and update the decisions log below if there's a non-obvious design choice worth recording.
8. **Update `.importlinter`** if the new adapter needs a contract line to stay independent of services or sibling adapters.

## Decisions log

Non-obvious design choices worth preserving:

- **Three sibling adapters for RetroDECK/RetroArch-side config, not one bundle.** The plugin has `RetroDeckPathsAdapter` (reads `retrodeck.json`), `RetroArchConfigAdapter` (reads `retroarch.cfg`), and `RetroArchCoreInfoAdapter` (reads `.info` files) as three independent adapters. A single combined "RetroDECK/RetroArch config" adapter would conflate three different owners (RetroDECK team vs RetroArch team vs libretro core maintainers), three different change triggers (user configurator edits vs runtime cfg writes vs Flatpak core releases), and three different file layouts (user home for `retrodeck.json`, RetroDECK config directory for `retroarch.cfg`, Flatpak install tree for `.info`). Bundling them would produce a class with too many reasons to change; splitting them keeps each adapter small, testable, and cleanly scoped to one source. This is the applied form of the "one parser per source" principle for the RetroDECK/RetroArch-side of the codebase.

- **No TTL cache for `.info` reads.** `RetroDeckPathsAdapter` uses a 30-second TTL cache for `retrodeck.json` because that file can be edited by the user at runtime via RetroDECK's configurator. `.info` files live inside a read-only Flatpak install and only change when the Flatpak is updated, which in practice tears down the plugin process anyway. A simple per-instance dict cache (keyed by `core_so`, no expiry) is sufficient — plugin restart picks up any real change. `RetroArchConfigAdapter` currently reads `retroarch.cfg` uncached on every call; that's fine for today's low call frequency and can grow a cache later if needed.

- **No fallback from RetroArch parser to ES-DE label.** When `.info` lookup returns `None`, `MigrationService` returns `None` for the core name and the save-sort migration logs a warning and skips the affected files. This is deliberately stricter than the previous behavior (which returned the ES-DE label and silently built wrong paths for any core where label ≠ corename). Fail-loud beats silent corruption; real-world `.info`-missing cases can be diagnosed from the warning and addressed by adding candidate paths or a bundled fallback.

- **`core_so` is the full `.so` basename including `_libretro`.** `CoreResolver.get_active_core` returns `(core_so, label)` where `core_so` is e.g. `"snes9x_libretro"`, not `"snes9x"`. This is set by the regex `[\w-]+_libretro` when parsing `es_systems.xml`'s `<command>` elements. The `.info` filename is therefore `{core_so}.info` (e.g. `snes9x_libretro.info`), not `{core_so}_libretro.info`. This is a subtle naming quirk documented here so future parser users don't double the suffix.

- **RetroDECK-only path resolution, standalone RetroArch out of scope.** The `.info` adapter only looks under `net.retrodeck.retrodeck` Flatpak paths (system-wide `/var/lib/flatpak/...` and per-user `~/.local/share/flatpak/...`). Support for `org.libretro.RetroArch` Flatpak, native RetroArch installs, and other launchers is deferred — a separate long-term issue tracks broadening the plugin's launcher support beyond RetroDECK, and this parser will be extended alongside that work.

- **Candidate paths use the `current/active` Flatpak symlinks.** Both candidate paths for `.info` files (system and per-user) route through `current/active`, which is Flatpak's stable symlink to the installed commit. This means the adapter does not need to know the specific Flatpak commit hash, and Flatpak updates do not break path resolution.

---

**Related pages:**

- [Backend Architecture](backend-architecture.md) — service/adapter architecture, dependency diagram, boundary enforcement
- [Save File Sync Architecture](save-file-sync-architecture.md) — save sync details, conflict detection, sort-by-core migration flow
- [RetroDECK Path Migration](../user-guide/retrodeck-path-migration.md) — user-facing guide for moving a RetroDECK install between storage locations
