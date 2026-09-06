# The live `es_systems.xml` stays the sole source; the vendored resolver is what reads it

## Status

Accepted. **Refines [ADR-0020](0020-live-es-systems-emulator-resolution.md):** the source is unchanged — ES-DE's live
`es_systems.xml`, no snapshot — and so is the rule that picks the default, but the reader is no longer the plugin's own
parser. `adapters/es_de_config.py` and its `CoreResolver` are deleted; the vendored
[emu-atlas](https://github.com/danielcopper/emu-atlas) resolver answers through `adapters/atlas_catalogue.py`.
ADR-0020's bake classifier (`domain/emulator_commands.py`), its precedence chain, and its `es_find_rules.xml` existence
probe are unchanged. **Carries forward [ADR-0012](0012-plugin-owns-core-selection-always-e-no-gamelist.md) §3** — the
precedence chain that removed the gamelist layer — into a world where the resolver reads that gamelist and states its
effect. Tracked under [#1840](https://github.com/danielcopper/decky-romm-sync/issues/1840), cut 3 of
[#1660](https://github.com/danielcopper/decky-romm-sync/issues/1660).

## Context

The plugin held two readers of ES-DE's `es_systems.xml`. Its own — an expat parser inside `CoreResolver` — answered the
emulator picker, the system-layer default, the libretro core the BIOS filter keys on, and the per-system accept-list.
The vendored resolver, adopted for firmware in #1660's first cut, answers the same questions from the same file.

Keeping both was not a steady state. The save cut that follows asks a **catalogue entry** where its emulator writes
saves (`entry.savefile_location(content_path)`), so with the plugin still resolving emulators through its own parser
that cut would have to map the plugin's labels onto the resolver's entries first — a third reader standing in as a
bridge. Two consumers already queued behind the same seam:
[#1652](https://github.com/danielcopper/decky-romm-sync/issues/1652) (launchability before a shortcut is baked) and
[#1701](https://github.com/danielcopper/decky-romm-sync/issues/1701) (per-entry container formats).

The two readers were not equivalent, and the differences are the interesting part.

**ES-DE's overlays.** ES-DE merges `<rd_home>/ES-DE/custom_systems/es_systems.xml` over the bundled file — a same-name
system is replaced whole, and a document-level `<loadExclusive/>` there makes ES-DE skip the bundled file entirely. The
plugin's parser read the bundled file alone, so a user with a custom system definition saw a default the frontend would
not have used.

**ES-DE's selections.** The resolver's entry order is the **effective** one: a per-game `<altemulator>` or a
system-level `<alternativeEmulator>` in the gamelist promotes an entry to the front of the answer. ADR-0012 put the
gamelist off every launch path deliberately, so effective order is exactly what this plugin must not follow.

**Empty means five things.** The parser's `{"available": False}` covered one of them: the file could not be found or
parsed. The resolver distinguishes the arrangement shipping no catalogue, a catalogue it has not located, one it could
not read, one whose readable layers are only part of the whole, and — the only statement about the machine — a catalogue
that was read and declares no emulator for this system.

## Decision

### 1. The seam is built per installation, and the plugin picks

`detect()` returns every arrangement it found in probe order (RetroDECK, EmuDeck, an unclaimed bare RetroArch flatpak, a
bare native RetroArch) and never picks a winner. `AtlasCatalogueAdapter` does not call it: it receives the chosen
installation through an injected `choose_installation` callable, and `bootstrap/adapters.py` wires that as **first
detected** — which is the RetroDECK answer wherever one is installed, because RetroDECK leads that order.

Nothing in `services/` learns which arrangement answered, and no atlas type crosses the adapter boundary. Offering the
others is Wave B ([#918](https://github.com/danielcopper/decky-romm-sync/issues/918)); when it lands, only the chooser
changes.

### 2. ES-DE's own selections are ignored; its overlays are honoured

The picker lists entries in **declared** order — `EmulatorEntry.declared_index` ascending, the shipped position that a
promotion never touches — and the system default is the first bakeable entry of that ordering, which is ADR-0020's
"first safely-bakeable command in document order" applied to the resolver's answer. `entry.selection`, which states why
an entry was promoted, is never read.

Two shapes make `entries[0]` and "index 0 exists" both wrong, so neither is assumed. Upstream mirrors ES-DE's own walk,
where an empty-text `<command>` holds a position without yielding an entry and a duplicate label takes none — so the
indices are ascending but may start above 0 and may skip. And an entry with **no** declared position sorts last: that is
the derived enumeration (`emulator-list-derived`), which no layer declared, and which cannot become the default anyway
because such an entry carries an empty command that the bake classifier reads as `no_rom_target`.

The overlay merge is the resolver's and is taken as it comes. `emulator-catalogue-exclusive` — a custom file declaring
itself the whole catalogue — is **not** a refusal: that answer is complete, and the code only says why it is small. A
user with a custom system definition can therefore see a different default than before, by design.

### 3. Honesty carries over, keyed on codes

`get_emulator_options` answers `{"available": False, "options": []}` when the catalogue answer carries any of
`emulator-catalogue-unavailable`, `-unestablished`, `-unreadable` or `-sealed`, and the picker renders "Emulator list
unavailable" — ADR-0020's wording, unchanged. An empty entry list carrying none of them comes back available, with an
empty option list: a real "this frontend knows no emulator for the system".

The test is the codes, never an empty `caveats`. A broken installation states its health findings on every answer it
gives, so a healthy-looking caveat list is not what "read, and it declares nothing" looks like. `sealed` is the one
refusal that may arrive **with** entries; the plugin suppresses them, because a list it knows is partial must not be
shown as the whole one. The codes are traced through the injected debug logger and no caveat `message` is ever parsed —
`code` is the stable half of that contract.

A fifth code joins the four, and it is the plugin reading an answer differently from the resolver on purpose.
`catalogue-invalid` means a catalogue file ES-DE refuses its whole load on — one that does not parse, or one with no
document-level `<systemList>` — so ES-DE runs with no systems and atlas states that truthfully as an empty enumeration.
Taken at face value the plugin would report "knows no emulator" for every platform, and `is_known_system` would answer a
positive `False` the candidate search reads as a denial. What the user has is one typo in one file, most likely their
own `custom_systems/es_systems.xml` — which the deleted parser never read, so this is new exposure — and it is also what
that parser answered: an unparsable file and a wrong root tag both yielded "unavailable". A file ES-DE loads fine that
simply declares no system stays a real "knows none"; the refusal is scoped to the invalid one.

The resolver raises on its own invariant violations rather than degrading, so every call is wrapped and a failure
becomes the same "unavailable" an unreadable catalogue gives. It is never an empty list.

### 4. One answer per system, per process, dropped on `reset_cache`

The adapter caches the chosen installation and every answer read through it: one catalogue answer and one `rom_location`
per system, plus the one `systems()` listing, which is per installation rather than per system. Measured on a RetroDECK
install: `emulators_for` 15–30 ms warm, `rom_location` ~430 ms on the first call and ~27 ms after, and `systems()` 16 ms
over 172 systems — and a per-platform core change fans out one resolution per installed ROM on the platform, so an
uncached read is paid per game. Caching the listing is also what keeps the candidate search where it was: the deleted
parser answered `is_known_system` out of its own cached parse.

**The invalidation story changed and is weaker on paper.** The old parser held an mtime guard, so a RetroDECK update
that rewrote `es_systems.xml` was picked up on the next read. What the resolver read to answer is now its own business
and no mtime is visible here, so a catalogue change lands on `reset_cache()` — which the per-platform core write already
performs — or on the next plugin reload. That is acceptable because the file changes on a RetroDECK update, which the
user performs deliberately and which does not race a running session.

Two things are deliberately **not** cached. A detection that found nothing is re-run, so a RetroDECK installed while the
plugin runs is seen without a reload — the every-call flatpak probe gave that for free and it is worth keeping. And the
standalone existence probe runs per call, so a component the user installs mid-session flips its option from
`needs_setup` to bakeable straight away.

### 5. `es_find_rules.xml` stays plugin-side, on its own seam

The resolver states the `%EMULATOR_<NAME>%` token a command names, never the host path it resolves to, so both questions
that need the path stay here: the sandbox launcher the folder-boot bake execs
([ADR-0019](0019-folder-as-launch-target.md)) and the installed-probe that downgrades a standalone RetroDECK does not
ship (ADR-0020 §2). They move out of the deleted module into `adapters/es_find_rules.py`, which locates the file itself
— probing the flatpak roots for `systems/linux/es_find_rules.xml` then `systems/unix/` — rather than resolving it beside
a catalogue whose path is no longer the plugin's to know.

`CoreInfoProvider` splits along that seam. It keeps the catalogue reads (`get_active_core`, `get_default_emulator`,
`get_emulator_options`, `reset_cache`); resolving a launcher becomes the call-shaped `SandboxLauncherFn`. One Protocol
spanning both would force one implementation to forward to the other and hide which source answered a given call.

One behaviour changes relative to the deleted parser: the find-rules probe now takes the **user** flatpak installation
before the system one. That is flatpak's own resolution order for an app, and it is the deploy the resolver reads the
catalogue from — so on a machine carrying both, the catalogue and the find rules now describe the same RetroDECK where
before they could describe two.

### 6. Nothing else moves

No schema change, no new callable, no arity change. The frontend picker payload keeps its shape (label, kind, `core_so`,
`is_default`, `bakeable`, `reason`), and the bake classifier `domain/emulator_commands.py` is untouched — whether a
command can be carried verbatim into a Steam shortcut's `-e` is a fact about this plugin's launcher, not about the
machine (upstream emu-atlas#84 may take part of it later). Stored pins keep resolving: the per-game pin is still a LABEL
on the `Rom` aggregate and the per-platform choice a LABEL in `settings.json`, and the resolver speaks the same ES-DE
labels.

## Consequences

- **One reader of `es_systems.xml`, and it is the audited one.** The plugin's parser and its tests are gone; a change to
  how ES-DE's catalogue is read is an upstream release and a checksum bump, not a diff here.
- **Overlays now count.** A user's `custom_systems/es_systems.xml` reaches the picker and the default. On a
  `<loadExclusive/>` overlay the catalogue is that file alone — complete, and usually much smaller than the bundled one.
- **A gamelist promotion is visible to the resolver and inert here.** Reading `selection` for ordering is the one change
  that would silently re-enable the gamelist as a launch input, and nothing mechanical stops it: `entries[0]` is the
  shorter spelling and gives exactly that.
- **A stale answer outlives a mid-session catalogue edit.** See §4 — the mtime guard is gone and `reset_cache()` or a
  reload is what replaces it.
- **`get_active_core` reads the resolver's `kind`, not the bake classifier's.** A libretro command the plugin cannot
  bake (a quoted MAME template, say) still names the core the BIOS filter is about, which is what the old
  `%CORE_RETROARCH%` search also did.

## Alternatives considered

- **Demote promoted entries by reading `selection`.** Rejected once `declared_index` existed (emu-atlas#391, shipped in
  v0.12.0): a demotion rule has to reconstruct where the entry came from, where the shipped position simply says. The
  request went upstream for that reason rather than being worked around here.
- **Let the adapter call `detect()[0]` itself, as the firmware adapter does.** Rejected. The firmware question is
  whole-machine by design and has one answer; the catalogue question is per installation, and #918 turns the choice into
  a user setting. Injecting the chooser now is the difference between a wiring change and an adapter rewrite later.
- **Ask `emulators_for(system, content_path=...)`.** Rejected. The content path is what lets a per-game `<altemulator>`
  promote an entry, and the plugin discards promotions — so passing it would buy a per-game answer to a system-level
  question and nothing else.
- **Keep an mtime guard of the plugin's own beside the resolver's read.** Rejected. It would have to stat a file the
  plugin no longer locates, and getting the wrong file (the bundled one, where an overlay is in force) makes the guard
  worse than none.

## Related

See also: [ADR-0020](0020-live-es-systems-emulator-resolution.md) (the live-`es_systems.xml` decision this refines),
[ADR-0012](0012-plugin-owns-core-selection-always-e-no-gamelist.md) (the plugin owns core selection; the gamelist is
never read as a launch input), [ADR-0019](0019-folder-as-launch-target.md) (the folder-boot direct bake that consumes
the sandbox launcher), [Core and Emulator Selection](../architecture/core-emulator-selection.md) (the resolver,
precedence, classifier and picker in detail), [Config Source Parsers](../architecture/config-source-parsers.md) (which
file each reader owns).
