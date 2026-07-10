# The sibling-group key is a connected component over RomM's sibling edges, keyed by canonical-source agreement

## Status

Accepted. Implements [#1368](https://github.com/danielcopper/decky-romm-sync/issues/1368). **Supersedes §1 of
[ADR-0021](0021-sibling-group-one-shortcut-binding-active-version.md)** — the coalesce-first key derivation and its
premise that it "produces byte-identical group membership" to RomM. Everything else in ADR-0021 stands: the
one-shortcut-per-group binding model (§2), the version resolution chain and canonical naming (§3), the per-`rom_id` save
model and the downloaded-version switch (§4), and the grandfathering of pre-existing duplicate shortcuts (§5). The key
is still persisted on `roms.sibling_group_key`, still scoped per platform, still `romm:<rom_id>:<platform_id>` for an
unmatched ROM; only how a matched ROM's key is _derived_ changes.

## Context

ADR-0021 §1 keyed each ROM by its **first non-null** external-metadata id in a fixed coalesce order (IGDB →
ScreenScraper → Moby → RA → Hasheous → LaunchBox → TGDB → Flashpoint), and asserted this mirrors RomM's own grouping. It
does not.

RomM's `sibling_roms` relation (`backend`, migration 0073 — metadata-only, verified on 4.9.2 and unchanged on
5.0.0-beta.3) relates two ROMs when they share **any one** non-null id over seven sources (the eight above minus
Flashpoint), scoped per platform. That is an **OR-join across sources**; the coalesce-first key is the **first** source
only. The two agree when metadata coverage is even, and diverge whenever it is uneven — which is the norm for regional
variants of one game whose US and EU releases carry different titles, because scrapers match them unevenly:

|                    | rom A (USA)     | rom B (Europe) |
| ------------------ | --------------- | -------------- |
| igdb_id            | 1001            | —              |
| ss_id              | 2002            | 2002           |
| hasheous_id        | 3003            | 3003           |
| launchbox_id       | 4004            | 4005           |
| coalesce-first key | `igdb:1001:<p>` | `ss:2002:<p>`  |

RomM lists A and B as siblings (shared ss/hasheous). The coalesce-first keys split them into two groups, so the plugin
minted two shortcuts and the version picker disabled the switch with a misleading "separate game entry" hint.

The naive repair — "merge whenever any id field agrees, refuse only when a field conflicts" — is **not viable**, because
**LaunchBox ids are per regional release**: A and B legitimately carry _different_ `launchbox_id`s while being the same
game. A low-priority source disagreeing is normal, not a conflict signal. The real signal is agreement (or absence) at
the **highest-priority** source the group shares.

## Decision

Key each fetched sync unit's ROMs by **connected components over RomM's own `sibling_roms` edges**, then key each
component by its **canonical source**. The kernel (`domain/sibling_group.compute_component_group_keys`, pure, stdlib
only) runs at sync time, before the shortcut build:

1. **Components.** Union-find over `sibling_roms` edges **between two fresh members** of the unit builds the connected
   components (processing sorted by `rom_id`, so the partition is independent of fetch order). A **resident** member —
   one that already carries a `sibling_group_key` (an incremental-reconstructed DB row, or a DB row outside the unit fed
   in as `resident_keys`) — is not unioned; instead its key, parsed to `{source}:{value}`, contributes a canonical
   **candidate** to the component it edges into. A `romm:` fallback key contributes nothing.
2. **Canonical source.** Per component, the canonical source is the **highest-priority** source (coalesce order) present
   on any member's ids or resident candidates.
3. **Agreement gate.** If the component holds **exactly one distinct value** at the canonical source, every fresh member
   gets `{source}:{value}:{platform_id}` — **including members that lack that source** (the Europe dump above, keyed on
   the USA dump's `igdb`). If it holds **multiple** distinct values (a genuine cross-game bridge smuggled in via a
   shared lower-priority id — e.g. `A igdb:1+ss:5`, `B ss:5+moby:9`, `C igdb:2+moby:9`, chained into one component whose
   IGDB values are `{1,2}`), no assumption-merge: every member falls back to its own coalesce-first key. A component
   with no metadata source at all falls back likewise (`romm:<id>:<platform>`).

**Sync-unit granularity.** Platform units fetch the full platform, so a group's whole membership is present — an
**authoritative** component view that recomputes the whole component each sync. Collection units fetch subsets, so a
member whose siblings live outside the subset is keyed **unit-locally** and converges when its platform unit runs (the
same acceptance ADR-0021 §3 already documents for canonical names; platform units run first in the work queue).
`resident_keys` — the persisted keys the orchestrator already reads (the bound-row registry the collapse diffs against
on the per-unit apply path, or every persisted non-null key via `iter_all()` on the preview path) — seed a fresh member
that edges into a DB-resident sibling with that sibling's canonical summary.

**One-time re-key.** Existing rows hold coalesce-first keys. Migration `011` NULLs every `sibling_group_key`; the
existing `needs_backfill` gate (a bound row with a NULL key forces its platform's incremental-skip to fall through to a
full fetch) then re-derives every key under the new kernel in one sync. NULL is a tolerated transient on the read paths
(the picker reads a NULL bound key as a solo group; the download guards treat it as its own group), so the window before
that sync is safe — no data loss, only a re-derivation.

**Membership predicate.** `target_in_sibling_group` stays the single authority the picker's `switchable` flag and
`switch_version` share. A **local** target is judged by key equality (the component keys now encode membership). A
**server-only** target (no local row yet) is judged by **canonical compatibility**: parse the bound key to
`{source}:{value}` and require the target's id at that `source` to be **absent-or-equal** — the persisted key doubles as
the group's canonical summary. A target that simply lacks the canonical id is in-group (it joins under the bound key on
switch-persist, and the next sync re-canonicalizes the whole component); a target carrying a _different_ value there is
a genuine metadata conflict and is rejected. A `romm:` bound key admits no server-only target (preserves the
pre-existing block). On switch-persist a server-only target adopts the **bound group's key**, not its own coalesce-first
key.

**The Flashpoint quirk, kept deliberately.** `flashpoint_id` is in the client's coalesce order but **not** in RomM's
seven-source `sibling_roms` view. So two Flashpoint-only twins share a coalesce-first key without any server edge — the
component kernel never sees an edge to merge them, but each keys solo to `flashpoint:<id>:<platform>` and they collide
onto the same key anyway. That reproduces ADR-0021's behavior exactly (a client-side coalesce that RomM does not model),
so it is kept and documented rather than "fixed" — dropping Flashpoint from the coalesce would split those twins, and
adding a synthetic edge would diverge from the server for no observed benefit.

## Consequences

- **The real bug is fixed.** Regional variants with uneven coverage land in one group, one shortcut, and switch freely —
  matching RomM's own grouping. Two pre-existing shortcuts of such a game collapse via the already-built grandfathering
  path (ADR-0021 §5); nothing user-visible is deleted.
- **The remaining disabled picker row is always a genuine metadata conflict**, so its copy now says so ("conflicting
  metadata match in RomM" / "fix the match in RomM") instead of the misleading "separate game entry".
- **The key tracks server-side re-matching live.** Because the key rides the sync UPSERT and is recomputed from server
  edges each platform sync, fixing a match in RomM re-groups on the next sync.
- **A collection-only member can key differently than its platform unit would**, converging when the platform runs — the
  documented partial-view acceptance, unchanged from ADR-0021 §3.
- **The one-time re-key costs one full refetch** per platform on the first sync after upgrade (the `needs_backfill`
  path), then the incremental skip resumes.

## Alternatives considered

- **Mirror RomM exactly — merge on any shared id.** Rejected: one wrong scraper match (a shared id between two genuinely
  different games) would bridge them into one group and one shortcut, and RomM's OR-join has no agreement gate to catch
  it. The canonical-source agreement gate is the safety valve the raw relation lacks.
- **Merge unless a field conflicts (strict all-field consistency).** Rejected: falsified by LaunchBox's per-regional-
  release ids — real siblings legitimately disagree at a low-priority source, so a conflict there is not a cross-game
  signal and would wrongly refuse to merge the exact case #1368 exists to fix.
- **Fix only the copy, leave the split.** Rejected: relabeling the disabled row is honest about the symptom but leaves
  the switch broken and the duplicate shortcut standing — the grouping itself has to change.

See also: [ADR-0021](0021-sibling-group-one-shortcut-binding-active-version.md) (the binding model this re-keys),
[ADR-0007](0007-rom-retention-identity-anchor.md) (the `roms` identity row the key lives on),
[Database Design](../architecture/database-design.md) (the `roms.sibling_group_key` column and the migration list).
