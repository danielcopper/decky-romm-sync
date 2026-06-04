# RomInstall layout — always a launch `file_path`, an optional `rom_dir` for folder-backed ROMs; the per-file `RomFile[]` model is deferred but additive

## Status

Accepted. Refines the `RomInstall` aggregate (introduced for the per-ROM split in
[ADR-0002](0002-per-rom-table-per-aggregate-split.md), schema in #780) by fixing the `install_path` dual-meaning that
the JSON→SQLite remodel (#784) introduced, and records the **per-file `RomFile[]` model as the committed but deferred
target**.

## Context

The legacy JSON install record (`InstalledRomEntry`) carried `file_path` always, plus `rom_dir: NotRequired[str]` —
`rom_dir` was set **only** for ROMs extracted from a multi-file archive; a single-file ROM had no `rom_dir` and its
containing directory was inferred from `file_path`. Presence/absence of `rom_dir` _was_ the single-vs-multi signal.

The SQLite remodel renamed `rom_dir` → `install_path` and made it `NOT NULL`. To keep it always-populated, a single-file
install's `install_path` was back-filled with `os.path.dirname(file_path)` — i.e. the **shared system directory**
(`<roms>/<system>/`), the dir that holds _every_ ROM of that platform — while a multi-file install's `install_path` is
its **dedicated extract directory** (`<roms>/<system>/<romname>/`). One field, two meanings.

That forced every consumer to _re-derive_ "is this multi-file?" from path shape: migration used
`install_path != dirname(file_path)`, removal used `is_safe_rom_path`'s segment-depth. The path-shape test is **wrong
for the common flat multi-file case** — the launch file (often an auto-generated `.m3u`) sits directly in the extract
dir, so `dirname(file_path) == install_path` exactly as for a single-file ROM. A RetroDECK-home migration therefore
moved only the launch file and orphaned the sibling disc/update/DLC files (the data-loss bug that surfaced this
decision).

Forward context (more multi-file work is coming, and we want the shape right): upstream RomM models this domain as `Rom`
(the logical game / launch identity) + `RomFile[]` (one row per physical file, each tagged with a `category`:
`game`/`dlc`/`update`/`mod`/`patch`/`manual`/…). Single-vs-multi is **derived** from the file rows there, never stored.
On disk RomM uses folder-per-game for multi-file and a bare file for single-file — exactly this plugin's layout. The
features that would need the richer model are tracked and **not yet actionable**: per-file download/resume + WiiU (#140,
currently **blocked** on a version-aware RomM API gateway #143) and multi-emulator / standalone Eden (#129), where the
#837 reporter's "base→`roms/`, updates+DLC→`storage/<game>/`" placement lives.

## Decision

- **`RomInstall` carries `file_path` (always) and `rom_dir` (nullable).**
  - `file_path` is the **launch target** — the file baked into the Steam shortcut's `launch_options`
    (`flatpak run … "<file_path>"`) that the `rom-launcher` exec wrapper is handed and runs (per
    [ADR-0009](0009-launcher-pure-exec-wrapper-baked-launch-options.md), which superseded the dynamic SQLite read of
    [ADR-0005](0005-launcher-resolves-path-from-sqlite.md)); save-path resolution, ES-DE core resolution, and the
    displayed filename all derive from it. It is load-bearing for every ROM and is never NULL.
  - `rom_dir` is the **dedicated per-ROM directory**, set only for folder-backed (multi-file) ROMs. It is **NULL for
    single-file ROMs**, which live as a bare file directly in the shared `<roms>/<system>/` dir and own no folder.
- **Rename `install_path` → `rom_dir`.** Single-vs-multi is read from `rom_dir` presence — never re-derived from
  `dirname(file_path)`, never stored as a separate boolean (an `owns_install_dir`-style flag is rejected; the folder's
  presence _is_ the flag).
- **Consumers branch on `rom_dir` presence.** Migration moves the whole `rom_dir` when set, else just the file.
  Uninstall `remove_tree`s `rom_dir` when set, else deletes the file; `is_safe_rom_path` stays as the path-containment
  guard, no longer the single-vs-multi discriminator.
- **Schema: `rom_dir TEXT` (nullable)** in `001_initial.sql`, renamed from `install_path TEXT NOT NULL`. #784 is the
  first release that creates the database (DB-init is wired in #784, commit `0122364`), so this is an edit to
  `001_initial.sql`, **not** a new migration. `NULL` (not `""`) for the single-file "no dedicated folder" case, per the
  schema's NULL-for-meaningful- absence rule.
- **The per-file `Rom` + `RomFile[]` + `category` model is the committed target, NOT built in #784.** It is deferred to
  the feature that needs it (#140; related #129). Keeping `file_path` and `rom_dir` as two **distinct** fields preserves
  RomM's launch-identity / physical-files seam, so a future `rom_files` 1:N child table (keyed `rom_id`,
  `ON DELETE CASCADE`, mirroring the existing `rom_save_files` child) is a purely **additive** `002_*.sql` migration
  plus a re-sync — not a rewrite. `rom_dir` then names the directory those `rom_files` rows live under; `file_path`
  stays the launch target.

## Consequences

- The migration multi-file data-loss bug is fixed at the **model** level — no path-shape heuristic remains to get wrong.
- `rom_removal.py` simplifies: a `rom_dir`-presence branch instead of the `is_safe_rom_path` depth trick (which only
  ever worked because single-file `install_path` happened to be the one-segment shared dir).
- Single-vs-multi reads off one nullable field — self-documenting; the dual-meaning `install_path` is gone, and so is
  the redundant shared-dir value stored for single-file ROMs.
- The richer `RomFile[]` model is **documented and tracked** (this ADR, the #140 issue thread, `database-design.md`) so
  it is not forgotten; it lands additively when #140 unblocks. Until then a multi-file install keeps "one launch file +
  its folder," which is sufficient for the current single-launch-target behavior (multi-disc via `.m3u`, base game for
  Switch) — it does **not** yet model per-file categories or multi-destination placement.
- Vocabulary (`file_path` = launch target, `rom_dir` = dedicated folder) is recorded in `CONTEXT.md`.
- A separate latent bug — the ES-DE per-game core override writes the gamelist `<path>` as a bare basename, which is
  already wrong for folder-backed ROMs — is tracked as its own follow-up and is **not** fixed here.

## Alternatives considered

- **`install_path` always populated (the status quo).** Rejected: dual meaning (shared vs dedicated), forces a
  path-shape multi-file heuristic that is wrong for flat archives (the bug), and the stored value is redundant with
  `dirname(file_path)` for single-file ROMs.
- **An `owns_install_dir` boolean flag.** Rejected: re-introduces a stored signal that the optional `rom_dir` already
  encodes by presence. It sounds right but the folder itself is the signal.
- **`file_path` optional, present only for multi-file.** Rejected: `file_path` is load-bearing for _every_ ROM (the
  launcher selects it; saves, core resolution, filenames, and the migration relocation anchor all derive from it) — it
  cannot be NULL for any type.
- **A dedicated per-ROM folder for _every_ ROM (single included), so `rom_dir` is always set.** Technically viable — the
  launcher is path-explicit and ES-DE scans subfolders recursively — but rejected: it diverges from RomM's and ES-DE's
  bare-file-for-single convention, adds a folder layer to every game, reworks placement + removal for no current
  benefit, and the genuine future needs are better served by the additive `RomFile[]` seam than by foldering.
- **Build `Rom` + `RomFile[]` + `category` now, in #784.** Rejected for this PR: the forcing feature (#140) is blocked
  on #143; the table's columns (category? per-file destination? download/resume state?) are undefined until #140/#129
  are designed; and the `user_version` framework makes it a zero-penalty additive migration later. Building it now would
  be unread, likely-wrong scaffolding on a near-complete cutover. It is adopted as the documented target instead, so the
  decision is recorded without paying for it prematurely.
