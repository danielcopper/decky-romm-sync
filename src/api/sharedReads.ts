/**
 * Reads that two independent stores issue for one ROM at the same moment.
 *
 * A game page has two stores — `components/panelState.ts` for the info panel,
 * `utils/gameDetailStore.ts` for the play row — and each loads some of the same
 * facts about the same ROM, so opening a page pays for those reads twice. One
 * store is the real answer (#994); until then a read issued while the same read
 * for the same ROM is still in flight joins it instead of opening a second
 * round-trip.
 *
 * **The admission rule.** Everyone awaiting one request is handed the SAME
 * answer, so a call site may join only if its answer cannot change between the
 * moment the first caller issued the read and the moment this caller joins it.
 * Nothing mechanical enforces that — this sentence is the module's whole safety
 * argument, and a new entry below is a claim that it holds for that read at that
 * call site. Only reads, never a write.
 *
 * What the rule excludes:
 *
 * - **The change-driven handlers.** `core_changed` and `metadata` re-read
 *   BECAUSE the thing changed, so joining a read issued before the change hands
 *   them the pre-change answer. Both keep calling `api/backend` directly.
 * - **`get_save_slots` / `is_save_tracking_configured`.** Their one refresh path
 *   (`refreshSlotState` in `components/panelState.ts`) is also the slot lane's
 *   re-read after a slot switch, delete or copy-to-slot — each of which
 *   dispatches `save_sync` the moment it completes. A post-mutation re-read must
 *   never join a pre-mutation one, so these two are absent from this module
 *   entirely and their duplicate on page open stands.
 *
 * What the rule admits, for the two reads below: both load lanes read them at
 * page open, and the only other paths that re-issue them are the two
 * `reloadDetail` triggers — `download_complete` and `rom_adopted`. Neither can
 * change a ROM's metadata or its active core, so the answer they would join is
 * the answer they would get. A version switch re-keys to a different rom_id,
 * which is a different key here and so never shares at all.
 */

import { getPlatformCoreInfo, getRomMetadata } from "./backend";

/** Every wrapped read's open-request map, so the test reset below can reach all
 *  of them without each read having to register itself. */
const _openRequests: Array<Map<unknown, unknown>> = [];

/** Wrap a single-argument read so overlapping calls for one argument share a
 *  single request. The entry is released the moment that request settles, so a
 *  later call reads again — this collapses concurrent callers, it does not
 *  cache. (`utils/cachedGameDetailStore.ts` is the one that caches, with a TTL.) */
function shareInFlight<A, R>(read: (arg: A) => Promise<R>): (arg: A) => Promise<R> {
  const inFlight = new Map<A, Promise<R>>();
  _openRequests.push(inFlight as Map<unknown, unknown>);
  return (arg: A): Promise<R> => {
    const open = inFlight.get(arg);
    if (open) return open;
    const request = read(arg);
    inFlight.set(arg, request);
    // Both arms are the same bookkeeping, and giving the release a rejection arm
    // is what keeps this branch from surfacing as an unhandled rejection — the
    // rejection callers see is the one on `request` itself.
    const release = () => {
      if (inFlight.get(arg) === request) inFlight.delete(arg);
    };
    request.then(release, release);
    return request;
  };
}

/** `get_rom_metadata` for one ROM, shared across the page's two load lanes. */
export const getRomMetadataShared = shareInFlight(getRomMetadata);

/** `get_platform_core_info` for one ROM, shared across the page's two load
 *  lanes. Both issue it unconditionally on every page open, so this is the one
 *  read that doubled on every single one. */
export const getPlatformCoreInfoShared = shareInFlight(getPlatformCoreInfo);

/** Test-only: forget every open request. A request only ever releases itself by
 *  settling, so a test that leaves one pending would otherwise hand it to the
 *  next test that reads the same ROM. */
export function _resetSharedReadsForTests(): void {
  _openRequests.forEach((requests) => requests.clear());
}
