/**
 * Module-level owner of the two panel-wide sync facts: the library/run stats
 * (`get_sync_stats`) and the live session-budget reading
 * (`get_session_budget_status`).
 *
 * Both were read straight from `api/backend` by `components/MainPage.tsx`, from
 * four call sites for the stats and three for the budget, each with its own
 * `.then(setState)` and none aware of the others. Two consequences, and the
 * first was a defect: an older answer could land after a newer one and overwrite
 * it — press Force Full Sync while the paused poll has a read open and the
 * pre-clear counts win, reverting the Last-sync line and the paused derivation.
 * And because the panel owned the state, leaving the page for any submenu threw
 * it away, so coming back re-issued every read and displayed nothing until they
 * landed.
 *
 * Owning both facts here fixes both. The store holds the data; the POLICY about
 * when to ask stays with the caller — MainPage keeps its 5s/10s interval and
 * decides when a re-read is worth issuing.
 *
 * Two properties carry it:
 *
 * **Responses apply in issue order.** Every read takes a sequence number the
 * moment it is ISSUED, and applies its answer only if no later-issued read for
 * that fact has already applied one. Arrival order is then irrelevant: whichever
 * of the callers won the race, the answer that stands is the one asked for last.
 * A read that REJECTS applies nothing and moves nothing, so an older read still
 * open remains free to answer — a failed read is not an answer, and leaving the
 * display on the last successful one is the same thing every call site did
 * before.
 *
 * **Concurrent refreshes share one request — except a read the change itself
 * provoked.** A refresh issued while one is open joins it rather than opening a
 * second round-trip, under the admission rule stated in `api/sharedReads.ts`:
 * joinable only where the answer cannot change between the moment the first
 * caller issued the read and the moment this one joins it. Both facts here are
 * whole-plugin reads taking no argument, so the join needs no key.
 *
 * What that rule excludes is every read issued BECAUSE the fact just changed.
 * The open read such a caller would join was issued before the change, so
 * joining hands it the pre-change answer as though it were the post-change one —
 * the very overwrite the issue ordering exists to prevent, except permanent: a
 * change-driven read is typically the last one its call site will issue, so no
 * later answer follows to correct it. Those callers take the `…AfterChange`
 * twin, which issues its own read and, by being issued last, supersedes the one
 * already open for every later joiner too. The rule decides this, not a list of
 * call sites: a new caller re-reading because something changed takes the twin,
 * whatever it was that changed.
 *
 * Read by `components/MainPage.tsx` through {@link useSyncStats} and
 * {@link useSessionBudget}; nothing else consumes either fact.
 */

import { useSyncExternalStore } from "react";
import { getSessionBudgetStatus, getSyncStats, logError } from "../api/backend";
import type { SessionBudgetStatus, SyncStats } from "../types";

const _listeners = new Set<() => void>();

function notify(): void {
  _listeners.forEach((fn) => fn());
}

/** One fact's stored answer plus the bookkeeping that orders its reads. */
interface ReadLane<T> {
  /** The newest applied answer, or `null` until one lands. Handed out as the
   *  `useSyncExternalStore` snapshot, which React compares by identity, so it is
   *  only ever REPLACED, never written into: an in-place write would leave a
   *  subscriber unable to tell the answer moved. The mirror-image hazard is a
   *  getter that allocates — hence {@link getSyncStatsSnapshot} and
   *  {@link getSessionBudgetSnapshot} hand this field back as it stands. */
  value: T | null;
  read: () => Promise<T>;
  /** Sequence number of the most recently issued read. */
  issued: number;
  /** Sequence number of the newest read whose answer has been applied. */
  applied: number;
  /** The open read a concurrent refresh may join, or `null` when none is open.
   *  Never rejects — a failure is logged where it happens, so a joiner cannot
   *  inherit an unhandled rejection from a caller it never met. */
  open: Promise<void> | null;
  /** Prefix of the log line a failed read writes. */
  failureLabel: string;
}

const _stats: ReadLane<SyncStats> = {
  value: null,
  read: getSyncStats,
  issued: 0,
  applied: 0,
  open: null,
  failureLabel: "Failed to load sync stats",
};

const _budget: ReadLane<SessionBudgetStatus> = {
  value: null,
  read: getSessionBudgetStatus,
  issued: 0,
  applied: 0,
  open: null,
  failureLabel: "Failed to load session budget status",
};

/** Install an answer unless a later-issued read has already answered. */
function applyAnswer<T>(lane: ReadLane<T>, seq: number, value: T): void {
  if (seq <= lane.applied) return;
  lane.applied = seq;
  lane.value = value;
  notify();
}

/** Issue a fresh read for this lane and make it the one a concurrent refresh
 *  joins, superseding whatever was open. */
function issueRead<T>(lane: ReadLane<T>): Promise<void> {
  const seq = ++lane.issued;
  const request = lane
    .read()
    .then(
      (value) => applyAnswer(lane, seq, value),
      (e) => logError(`${lane.failureLabel}: ${e}`),
    )
    .finally(() => {
      // Identity-checked: a read superseded by a later one must not clear the
      // later one's slot when it finally settles.
      if (lane.open === request) lane.open = null;
    });
  lane.open = request;
  return request;
}

/** Join the open read for this lane, or issue one when none is open. */
function joinOrIssue<T>(lane: ReadLane<T>): Promise<void> {
  return lane.open ?? issueRead(lane);
}

/** The stats as last read, or `null` before the first answer lands. */
export function getSyncStatsSnapshot(): SyncStats | null {
  return _stats.value;
}

/** The session-budget reading as last read, or `null` before the first answer lands. */
export function getSessionBudgetSnapshot(): SessionBudgetStatus | null {
  return _budget.value;
}

export function onSyncStatsStoreChange(fn: () => void): () => void {
  _listeners.add(fn);
  return () => {
    _listeners.delete(fn);
  };
}

/** Subscribe a component to the stats. Renders the last known answer
 *  immediately — the store outlives the panel, so returning from a submenu no
 *  longer blanks the display while a fresh read is in flight. */
export function useSyncStats(): SyncStats | null {
  return useSyncExternalStore(onSyncStatsStoreChange, getSyncStatsSnapshot);
}

/** Subscribe a component to the session-budget reading. */
export function useSessionBudget(): SessionBudgetStatus | null {
  return useSyncExternalStore(onSyncStatsStoreChange, getSessionBudgetSnapshot);
}

/** Ask for the stats. Joins a read already open — for a caller with no reason to
 *  believe they just changed. Resolves once this refresh has been answered or
 *  has failed; a failure is logged here, never thrown at the caller. */
export function refreshSyncStats(): Promise<void> {
  return joinOrIssue(_stats);
}

/**
 * Re-read the stats BECAUSE they just changed — a run reaching a terminal stage,
 * which rewrites `last_sync`, `last_attempt` and the counts, and Force Full
 * Sync's clear of the per-platform stamps.
 *
 * Never joins an open read: this is the case the admission rule in
 * `api/sharedReads.ts` excludes, spelled out in this module's docstring. Both
 * callers are also the last read their path issues — for a completed run the
 * poll interval stops on the same tick — so a joined pre-change answer is not
 * merely wrong for a moment, it is the last thing the panel shows.
 */
export function refreshSyncStatsAfterChange(): Promise<void> {
  return issueRead(_stats);
}

/** Ask for the live session-budget reading. Joins a read already open — the
 *  mount read and the poll tick both simply want the current reading, and
 *  neither follows a change to it. */
export function refreshSessionBudget(): Promise<void> {
  return joinOrIssue(_budget);
}

/**
 * Re-read the session budget BECAUSE the run whose consumption it reports just
 * ended. The terminal stage is what turns a climbing mid-run reading into the
 * run's end state — the figure the paused / high-heap banners are about — so the
 * same exclusion applies: a reading taken a round-trip before the run ended
 * answers the old question. It sticks, too. While a run is live the 5s poll
 * keeps a read open for this one to join, and that poll stops on the terminal
 * frame, so for a completed run a joined mid-run figure is the last reading the
 * panel ever shows.
 */
export function refreshSessionBudgetAfterChange(): Promise<void> {
  return issueRead(_budget);
}

/** Reset the module state between tests. Not for production use. A read only
 *  ever releases its slot by settling, so a test that leaves one pending would
 *  otherwise hand it to the next test's first refresh. */
export function resetSyncStatsStoreForTests(): void {
  resetLane(_stats);
  resetLane(_budget);
  _listeners.clear();
}

function resetLane<T>(lane: ReadLane<T>): void {
  lane.value = null;
  lane.issued = 0;
  lane.applied = 0;
  lane.open = null;
}
