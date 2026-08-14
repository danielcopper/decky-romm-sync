# A RomM sibling group is one game: one Steam shortcut per group, and the bound sibling is the active version

## Status

Accepted. Tracked under [#1267](https://github.com/danielcopper/decky-romm-sync/issues/1267) (slices #1295–#1298).
Extends [ADR-0009](0009-launcher-pure-exec-wrapper-baked-launch-options.md) (the baked-`launch_options` model supplies
the appId-safe switch mechanism) and [ADR-0007](0007-rom-retention-identity-anchor.md) (`roms` as the permanent identity
row). The version-switch UI slice reuses the per-game deviation template of
[ADR-0011](0011-per-game-core-override-in-db-applied-via-e-flag.md) /
[ADR-0014](0014-per-game-disc-selection-in-db-applied-as-bake-time-launch-path-override.md).

## Context

A game frequently exists in a RomM library as several dumps of the same title: region versions (`(USA)`, `(Europe)`,
`(Japan)`), multi-language dumps (`(En,Fr,De)`), revisions (`(Rev 1)`), and patched or virtual-console variants. RomM
models this natively (verified against 4.9.2, API + server source):

- Every ROM carries `sibling_roms` — all other versions of the same game. Two ROMs are siblings when they matched the
  same metadata id, coalesced in a fixed order (IGDB → ScreenScraper → Moby → RA → Hasheous → LaunchBox → TGDB →
  Flashpoint), scoped per platform. Unmatched ROMs have no siblings.
- The version dimensions are structured fields on each ROM: `regions`, `languages`, `revision`, `tags`.
- A per-user default version exists: `rom_user.is_main_sibling` (RomM's "SET DEFAULT" checkbox). It is optional, and
  RomM's own grouped views fall back to alphabetical `fs_name_no_ext` when unset.
- Server-side saves are strictly per ROM id — sibling versions have independent save universes. This matches both the
  plugin's per-rom_id `RomSaveSyncState` keying and RetroArch's per-content save naming (the `.srm` is named after the
  ROM basename, so two dumps never share a save file on disk either).

The plugin is sibling-blind today: the fetch receives all of these fields and persistence drops every one. That is not
merely a missing feature — it produces wrong behavior. The Steam appId is `CRC32(exe + appName)` and the launcher exe is
a constant, so **same-named siblings collide onto one appId**: Steam keeps a single shortcut, the roms table's
one-binding-per-appId rule unbinds whichever sibling rows lost the race, and the binding lands on the **last-committed**
sibling nondeterministically while the remaining rows permanently read as unsynced. Siblings whose display name differs
(a Japanese dump with its native title) instead become **duplicate shortcuts** for the same game.

> **Errata (2026-07).** The `appId = CRC32(exe + appName)` premise in this Context is disproven on current Steam — the
> appId is **assigned at creation**, not hashed. Without a hash, same-named siblings do not collide onto one appId; they
> instead produce **duplicate shortcuts** (the same failure the differing-name case already describes). The decision
> below is unaffected: one shortcut per sibling group prevents both manifestations. Corrected model:
> [Steam Non-Steam Shortcuts — App IDs and Artwork](../architecture/steam-non-steam-shortcuts.md#app-ids-and-artwork).

## Decision

### 1. Group identity is computed client-side, mirroring RomM's key; the fetch stays ungrouped

> **Superseded by [ADR-0022](0022-component-based-sibling-group-key.md)
> ([#1368](https://github.com/danielcopper/decky-romm-sync/issues/1368)).** The **key-derivation** decision below —
> coalesce-first (the ROM's own first non-null metadata id) — and its premise that this "produces byte-identical group
> membership" to RomM are reversed: the key is now a **connected component over RomM's `sibling_roms` edges**, keyed by
> the highest-priority source the component _agrees_ on (uneven coverage merges; a genuine cross-game bridge falls
> back). Everything else here still holds — the key is still persisted on `roms.sibling_group_key`, still scoped per
> platform, still `romm:<id>:<platform>` for an unmatched ROM, still rides the sync UPSERT, and the fetch still stays
> flat/ungrouped (the `group_by_meta_id` rejection below is unchanged). Where this section says "coalesce-first key,"
> read ADR-0022.

A pure domain function derives each ROM's sibling-group key from the same coalesced metadata id RomM partitions by (IGDB
→ SS → Moby → RA → Hasheous → LaunchBox → TGDB → Flashpoint, scoped per platform), falling back to the ROM's own id — an
unmatched ROM is a solo group, exactly as on the server. The key is persisted on `roms` together with the version
dimensions (`regions`, `languages`, `revision`, `tags`) and `is_main_sibling`. All of these are server-derived facts, so
— unlike the user-pin columns `emulator_override` / `selected_disc` — they are **included** in the sync UPSERT and
refresh on every sync.

The library fetch does **not** use RomM's `group_by_meta_id` parameter. Grouping server-side would drop every non-
representative sibling from the fetched list: an installed non-default version would vanish → the stale path would tear
its shortcut down (removal churn), and the version picker would need per-sibling detail fetches anyway because the
grouped response carries only slim sibling stubs. Fetching flat and grouping locally keeps every sibling's full version
metadata in hand and keeps installed versions visible to the diff, while producing byte-identical group membership.

### 2. One Steam shortcut per group; the binding is the active version

The sibling group is the unit that maps to a Steam shortcut. The existing one-binding-per-appId rule is promoted from
accident to model: the sibling row holding `shortcut_app_id` **is** the active/installed version of that game. Sibling
rows without a binding are group members, not unsynced strays. The sync diff and shortcut lifecycle are keyed by group:
a group is synced when one member is bound; a group gone from the server removes the shortcut; a bound sibling that
disappears while the group survives **rebinds** to the surviving representative instead of removing the shortcut.

Shortcut identity is **sticky**: name and appId are minted from the representative at shortcut creation and never change
automatically afterwards — not on a version switch, not when the server-side default changes. Switching the active
version flips only the baked `launch_options` (appId-safe, hardware-validated in #827) and moves the binding to the
target row; artwork, collections, playtime and the Steam appId all survive. Renaming is what would change the appId
(delete + recreate churn), so it never happens implicitly.

### 3. Version resolution chain: installed > existing binding > RomM default > 1G1R ranking; the choice stays local

Wherever one version must be chosen (shortcut representative, picker preselect), the chain is: an installed sibling
wins; else an existing binding; else the server-side `is_main_sibling` default (respected read-only); else the **1G1R
ranking** — prerelease demotion, then **region priority**, then newest revision, then alphabetical `fs_name_no_ext`,
then `rom_id`. The first three legs are membership filters; the rest are the total order applied inside the surviving
leg. The alphabetical leg is exactly RomM's own fallback, so an ungroomed retail library with no region signal behaves
identically to the RomM web UI. The user's version choice is expressed solely through which sibling is bound/installed —
**no write-back** of `is_main_sibling` to the server. Writing it would require the `roms.user.write` scope, and a scope
change invalidates every user's token (forced re-sign-in) — too high a price for mirroring a preference RomM already
lets the user set in its own UI.

> **Amendment (1G1R ranking + canonical naming, same PR as the region-priority slice).** The chain above originally
> ended at `alphabetical`. In practice the alphabetical leg binds — and, because the shortcut name is minted from the
> winner and is **sticky forever**, permanently names — the wrong dump: a release whose display name is in another
> script can win the leg, and the shortcut then carries that name for good. These amendments, all local-only, no new
> server scope:
>
> - **1G1R ranking hardening — prerelease demotion + revision (igir / No-Intro convention).** The total order the
>   fallback legs apply gained two dimensions bracketing the region leg. **Prerelease demotion ranks FIRST, before
>   region:** a member is prerelease when any of its structured `tags` names a draft build — Alpha, Beta (incl. numbered
>   `Beta 1` / `Beta 2`), Proto, Sample, Demo (case-insensitive, tolerant of a trailing number) — and is demoted below
>   **every** retail sibling regardless of region, so a finished `(Japan)` release beats a `(USA) (Beta)` (finals before
>   prereleases, **across regions** — the cross-region rule). `Unl`, `Aftermarket`, collection-name tags and any unknown
>   tag carry no finished-vs-draft signal and are neutral (never demoted). **Newest revision ranks after region:**
>   within one region the higher `revision` wins (natural compare — `(Rev 3)` beats `(Rev 1)` beats the base dump; empty
>   = lowest), but the leg sits **below** region, so a `(USA)` base still beats a `(Europe) (Rev 9)`. The alphabetical
>   `fs_name_no_ext` leg stays last-but-one and also keeps a base dump ahead of a filename-only re-dump
>   (`(Virtual Console)`, `(Extended Edition)`) that RomM does **not** parse into a structured tag. The full fallback
>   order is therefore **prerelease demotion > region priority > revision (newest) > alphabetical > `rom_id`**. Like
>   region priority, both new legs are local-only, evaluated at resolution time, and shielded by an existing binding.
> - **Region-priority leg** (inserted before alphabetical). A version is ranked by its **best** `regions` entry against
>   a **fixed** build-time default order — `World > USA > Europe > Japan` (World first for explicit multi-region
>   international releases; USA before Europe to match the 1G1R convention and avoid PAL-50Hz dumps being the silent
>   default on older consoles), every other named region after these ranked alphabetically among themselves, and a
>   version with no region ranked last. This order is a fixed constant, **not** a language/system detection. A single
>   user override, the `preferred_region` setting (a bucket-1 user-intent config → `settings.json`, per ADR-0003;
>   default `"auto"` = the fixed order), lifts one region to the very top; the default order continues behind it. The
>   leg is evaluated **at resolution time** (during a sync's collapse), so the preference takes effect only on the next
>   sync — no live re-resolution, and an **existing binding shields its group** (the installed/binding legs win before
>   region priority is even consulted), which is exactly why changing the preference never disturbs already-synced
>   games. The domain stays pure: the preference enters `resolve_group_representative` (and the naming function below)
>   as an explicit parameter, read from settings in the service layer and threaded into every collapse call site
>   (preview + apply) as the same value within a run. The QAM dropdown that sets it is populated from **fixed anchors**
>   (Default, World, USA, Europe, Japan) plus the distinct regions read from the **local** `roms.regions` of the synced
>   library (no server scan), and a confirmation modal spells out the apply-at-next-sync / no-retroactive-rename
>   semantics before persisting.
> - **Canonical name follows the pure ranking, not the bound member** (mint-time only). `canonical_group_name` returns
>   the `name` of the member ranked first by the **pure** order (prerelease demotion > region priority > revision >
>   alphabetical > `rom_id`), ignoring the installed/binding/default filters — explicitly **not** majority voting (two
>   Japan dumps + one USA yields the USA member's name). A NEW group's shortcut is minted with this canonical name while
>   its `rom_id`/bind target stays the resolution-chain representative, so a Japanese _default_ still binds and launches
>   Japan but the shortcut carries the USA name (the "name can lag the active version" trade-off in Consequences, now
>   made deliberate for the region- preferred name). The appId is minted from the canonical name by the frontend
>   `AddShortcut`; the DB binding lands on the representative `rom_id`; the commit path is agnostic to the name↔rom
>   mismatch (it binds on `rom_id` + the acked appId, and persists each sibling's own RomM name). **Mint only:** an
>   already-bound group (grandfathered / rebind) carries its persisted bound name verbatim — emitting a different name
>   would flip `classify_roms` to "changed" and rename the live shortcut, changing its appId; that must never happen.
>   Under a partial (collection) view the canonical name is chosen among the fetched members only — acceptable and
>   documented, since the group's real representative rides its own platform unit in the same run.

### 4. Saves stay per rom_id; a version switch never migrates saves

Save-sync keying is untouched: each sibling keeps its own `RomSaveSyncState`, slots, baselines and server save universe.
After a version switch the game plays the target version's saves — matching what RomM itself does and what RetroArch's
per-content `.srm` naming produces on disk anyway. No automatic carry-over, ever (a foreign-looking save appearing on a
different dump is exactly the ambiguity the save-sync design refuses to auto-resolve). The save-status UI must make
unambiguous which version's saves are in play.

> **Amendment (downloaded-version switch + one-download-per-game, #1298 slice).** §2's binding-move switch now applies
> to **downloaded** games, not only uninstalled ones, and the "multiple versions may coexist on disk" consequence is
> capped. Three rules, all local-only, no new server scope:
>
> - **At most one downloaded version per game, enforced at download time.** Tapping Download on a version while another
>   member of the same sibling group is installed removes the older install first — via the canonical
>   `RomRemovalService.remove_rom` path (files + `rom_installs` row, **saves untouched** per ADR-0007) — then downloads.
>   No dialog. A member bound to a **different** shortcut (a grandfathered duplicate, §5) is exempt and never removed.
>   The picker's switch itself still deletes nothing; only an explicit Download supersedes an install. So "multiple
>   versions may coexist on disk" holds only until a second version is downloaded — after which switch-back to the
>   superseded version needs a re-download, which rejoins the saves the supersede left in place.
> - **Save-stranding guard on switch-away.** Switching away from a downloaded version whose local saves drift from their
>   sync baseline is **soft-blocked** so the user decides: sync-first-then-switch, switch-anyway (the saves stay on disk
>   and stop syncing until switch-back — never deleted or transferred, §4 unchanged), or cancel. The block fires only
>   when the bound version is installed **and** drifted; a purely-local switch (synced saves, or an uninstalled bound
>   version) is free and allowed **even offline**. Offline, the sync-first option is withheld (the saves can't be
>   uploaded), leaving switch-anyway / cancel. The read/close-then-fetch-then-short-write-UoW ordering (ADR-0006)
>   re-checks group membership and bound-elsewhere inside the write transaction (TOCTOU) so a download or rebind that
>   lands mid-flight decides.
> - **Block while a group download is in flight.** A switch is refused while any member of the group has an active
>   download (cancel it first). A paused download whose version is no longer the active binding is refused on resume and
>   its queue entry dropped, so a stale paused transfer can't resurrect a superseded install.

### 5. Existing duplicate shortcuts are grandfathered, not force-collapsed

Libraries synced before this model can hold multiple shortcuts of one group (different-name siblings). A group with
multiple **bound** shortcuts keeps them all — convergence to one-shortcut-per-group happens naturally when the user
uninstalls one. Duplicate shortcuts of _uninstalled_ siblings are removed by the normal stale path on the next sync.
Nondeterministic bindings (the same-name collision case) are normalized once by the resolution chain in §3. No migration
ever deletes a shortcut the user has played.

## Consequences

- **A real bug class is fixed, not just a feature added.** The nondeterministic last-sibling-wins binding and the
  permanently-unsynced phantom rows disappear; fresh syncs of an N-version game yield exactly one deterministic
  shortcut.
- **Version switching is cheap and lossless** — a `launch_options` flip plus a binding move. A version already on disk
  needs no re-download on switch-back (the #1298 amendment under §4 caps a game to **one** downloaded version at a time,
  enforced at download; a switch never removes files, only an explicit Download supersedes an install).
- **The shortcut name can lag the active version** (play the Japanese dump under the shortcut minted from the USA name).
  Accepted: name stability is what protects appId, artwork, collections and playtime.
- **Unmatched ROMs are untouched** — solo groups degrade to exactly today's per-ROM behavior.
- **The version metadata rides the sync UPSERT**, so server-side re-matching (a sibling joining or leaving a group)
  propagates on the next sync; the group-keyed diff must tolerate membership drift.
- The download picker (#1297) and the installed-game version switch (#1298) become thin UI over this model.

## Alternatives considered

- **Fetch with `group_by_meta_id=true` and let the server group.** Rejected: installed non-representative siblings
  vanish from the list (stale-removal churn on healthy installs), sibling stubs are too slim to build the picker without
  N extra requests, and incremental fetches get representative-flip semantics the diff would have to undo. Client-side
  grouping over the flat fetch yields identical membership with none of that.
- **One shortcut per ROM with name disambiguation** (append `(Germany)` etc. to make appIds unique). Rejected: it
  multiplies every multi-version game into N library entries — the exact clutter #1267 exists to remove — and renaming
  existing shortcuts churns their appIds once.
- **Write the user's choice back to `is_main_sibling`.** Rejected for now: requires the `roms.user.write` scope, and a
  scope bump forces every user through re-sign-in. The server default is respected read-only instead; revisit if a scope
  change becomes necessary for other reasons.
- **Automatic save carry-over on version switch.** Rejected: contradicts RomM's own per-version save model, and silently
  copying a save onto a different dump is the kind of assumption the save-sync design categorically avoids. If demand
  materializes, an explicit user-triggered copy (rename + upload to the target sibling) can be a separate feature.
- **Force-collapse pre-existing duplicate shortcuts in a migration.** Rejected: destructively removes shortcuts that
  carry artwork, collections and playtime, and picking the loser is guesswork — the removal-churn class of bug (#1036)
  this project has already paid for once. Grandfathering converges without deleting anything a user can see.

See also: [ADR-0007](0007-rom-retention-identity-anchor.md) (the `roms` identity row the binding lives on),
[ADR-0009](0009-launcher-pure-exec-wrapper-baked-launch-options.md) (baked `launch_options`, the switch mechanism),
[ADR-0011](0011-per-game-core-override-in-db-applied-via-e-flag.md) /
[ADR-0014](0014-per-game-disc-selection-in-db-applied-as-bake-time-launch-path-override.md) (the per-game deviation
template the switch UI reuses), [Database Design](../architecture/database-design.md) (the roms table this extends).
