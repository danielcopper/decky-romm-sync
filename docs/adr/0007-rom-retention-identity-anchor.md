# ROM retention — `roms` rows are identity anchors; auto-stale unbinds, only a deliberate purge deletes

## Status

Accepted. Refines [ADR-0002](0002-per-rom-table-per-aggregate-split.md) (the per-ROM table split + cascade FKs) by
fixing the _retention_ policy #780 deferred — specifically that the cascade FKs do **not** auto-fire.

## Context

The schema (`db/migrations/001_initial.sql`) gives every per-ROM child table (`rom_installs`, `rom_metadata`,
`rom_playtime`, `rom_save_sync_states`, `rom_save_files`) an `ON DELETE CASCADE` foreign key onto `roms`, with a comment
that `roms` rows are "deleted ONLY on a deliberate library prune (ROM genuinely gone from RomM)." But "genuinely gone
from RomM" is exactly what the **automatic** library sync detects (its `stale_rom_ids` pass) — so reading the comment
literally would make the cascade fire automatically and destroy a ROM's local **playtime and saves** on any transient
server blip, collection-visibility change, or reversible RomM edit. The confirmed domain priority is that local playtime
and save history **survive a ROM leaving RomM**.

## Decision

- The `roms` row is a permanent **identity anchor**, keyed by RomM's stable `rom_id`. **Transient absence — removing the
  Steam shortcut, uninstalling files, or the automatic sync finding the ROM no longer on RomM — never deletes the `roms`
  row.** It _unbinds_ (NULLs `shortcut_app_id` via `Rom.unbind_shortcut()`) and/or drops only the directly-affected
  child row (e.g. `rom_installs` on uninstall). Playtime, saves, and metadata persist.
- A cascade `DELETE` of a `roms` row (which atomically reaps every per-ROM child via the FKs) is reserved for a
  **deliberate purge** — an explicit, opt-in user action. That action **does not exist today** and is out of scope for
  the cutover; the cascade FKs sit dormant until it is built.
- **No time-based GC.** `roms` grows with every ROM ever seen; rows are tiny (single-digit MB over years even for a
  heavy curator), and the stable `rom_id` re-links cleanly on re-add. This is accepted over mirroring RomM exactly.

> **Correction (#1570).** The Decision stands — retention is what made the deliberate purge cheap to add — but two of
> the statements above are wrong as written and a third has been overtaken:
>
> - **`rom_id` does not re-link on re-add.** RomM issues a _new_ id when a ROM is deleted and rescanned, so a re-add
>   produces a **second row in the same sibling group** ([ADR-0022](0022-component-based-sibling-group-key.md)) rather
>   than reattaching to the retained one. The retained row keeps the local playtime and saves recorded under the old id,
>   and nothing carries them across on its own. Retention is still right — losing that history to a transient absence
>   would be worse — but "it re-links" was never the reason.
> - **There are six per-ROM cascade children, not five.** The Context above lists `rom_installs`, `rom_metadata`,
>   `rom_playtime`, `rom_save_sync_states` and `rom_save_files`. `rom_playtime_sessions` — the outbox of play sessions
>   not yet ingested by RomM, added in migration `006` — carries the same `ON DELETE CASCADE` and is missing from the
>   list. A purge therefore also discards queued sessions that exist nowhere else, which is part of why it captures
>   playtime before deleting.
> - **The deliberate purge now exists.** "That action does not exist today" was true when this was written. Removed-game
>   cleanup ([removed-game-cleanup.md](../architecture/removed-game-cleanup.md)) is that action: explicit, opt-in, and
>   authorized only by a fresh exact-id 404 from RomM. The cascade FKs are no longer dormant.

## Consequences

- The cascade FKs are real and correct but **never auto-fire** — a future reader seeing `ON DELETE CASCADE` with no
  automatic `DELETE` path should land here.
- "Playtime survives shortcut removal" generalizes to "local history survives a ROM leaving RomM."
- The `001_initial.sql` retention comment, ADR-0002, and `docs/architecture/database-design.md` get their wording
  sharpened (auto-stale = unbind; deliberate purge = delete) in the cutover PR (#784).
- If `roms` growth ever becomes a genuine concern, the middle ground is an opt-in GC over a precise "fully dead"
  predicate (no shortcut, not installed, server-stale, **and** no playtime/saves worth keeping) — never coupling
  deletion to the automatic sync.

## Alternatives considered

- **Auto-delete on sync-stale** (the literal DDL-comment reading). Rejected: an automatic, possibly-transient signal
  would cascade-destroy playtime/saves; a blip or reversible RomM change is then unrecoverable, and re-add starts fresh.
- **Pure never-delete** (no purge path at all). Not adopted as a hard rule — the cascade FKs stay so a deliberate purge
  can be added cheaply later; we simply never auto-fire them.
