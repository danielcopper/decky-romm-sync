/**
 * Module-level owner of the pending sync preview — the delta the backend has
 * computed and is holding, between the preview run that produced it and the
 * answer the user gives it (Apply, Dismiss, or a fresh Sync press).
 *
 * **Why this fact is not component state, when panel rendering state normally
 * is.** The preview outlives the panel instance that asked for it. `sync_preview`
 * is an awaited callable, so its answer is delivered to the closure of whichever
 * `MainPage` pressed Sync — and leaving the main page unmounts that instance
 * while the run keeps going. A `setPreview` in that closure then lands on a dead
 * component, and the instance actually on screen shows the idle Sync buttons
 * over a preview the backend is holding, inviting a second run on top of the
 * first. Storing the answer where the backend's own lifetime can reach it — a
 * module store, outliving every mount — is what makes the card survive.
 *
 * **This is not a second source of truth.** The backend owns the preview; this
 * store holds its answer verbatim and is only ever filled from it. Nothing here
 * derives, re-assembles, merges or repairs a preview, and no value that
 * `sync_preview` or `get_pending_preview` did not hand over can enter. What was
 * wrong was never that the panel rendered from state — it is that the *rendering*
 * state was frozen inside one component instance while the fact it renders is
 * owned by a process that outlives it.
 *
 * **Ordering.** Two kinds of writer meet here. A READ of the backend's staged
 * snapshot ({@link refreshPendingPreview}) answers a round trip after it was
 * issued, so it describes the world as it was at issue time. An ANSWER the user
 * just produced ({@link adoptPreview}, {@link clearPendingPreview}) is true the
 * moment it is written. Every write takes a ticket when its information was
 * ISSUED and applies only if no later-issued write has already applied. So a
 * dismiss beats a read already open — that read describes a world in which the
 * user had not answered yet — and the read can never put the card back behind
 * them. Between two reads the same rule holds: the one asked for last wins,
 * whichever answers first.
 *
 * The answer verbs are not absolute, and must not be: a read ISSUED AFTER a
 * discard legitimately outranks it, because by then the backend has been told
 * and is answering about what it holds now — a fresh preview computed since, or
 * nothing. So what protects a dismissal is not the verb being privileged, it is
 * the ordering: no read that was already open when the user answered can apply.
 * A verb that stopped taking a ticket would lose to every read in flight.
 *
 * **A read only ever FILLS, never clears.** `get_pending_preview` answers
 * `preview: null` for three different situations — nothing is staged, the
 * snapshot aged out, and it is being withheld because a run is in flight — and
 * the wire cannot tell them apart. A null answer is therefore not evidence that
 * a preview this store holds is gone. Dropping one is the job of the explicit
 * answer verbs alone; expiry is carried on screen by the card's own countdown.
 */

import { useSyncExternalStore } from "react";
import { getPendingPreview, logError } from "../api/backend";
import type { SyncPreview } from "../types";

/** The stored answer, or `null` when nothing is pending. Handed out as the
 *  `useSyncExternalStore` snapshot, which React compares by identity, so it is
 *  only ever REPLACED and the getter never allocates — a getter handing back a
 *  fresh object per call re-renders forever, and an in-place write would leave a
 *  subscriber unable to tell that the preview moved. */
let _preview: SyncPreview | null = null;
/** Ticket of the most recently issued write. */
let _issued = 0;
/** Ticket of the newest write whose value has been applied. */
let _applied = 0;
const _listeners = new Set<() => void>();

function notify(): void {
  _listeners.forEach((fn) => fn());
}

/** Install a value unless a later-issued write has already applied one. */
function apply(seq: number, value: SyncPreview | null): void {
  if (seq <= _applied) return;
  _applied = seq;
  if (_preview === value) return;
  _preview = value;
  notify();
}

/** The preview as it stands, or `null` when nothing is pending. */
export function getPendingPreviewSnapshot(): SyncPreview | null {
  return _preview;
}

export function onPendingPreviewChange(fn: () => void): () => void {
  _listeners.add(fn);
  return () => {
    _listeners.delete(fn);
  };
}

/** Subscribe a component to the pending preview. Renders what is pending
 *  immediately — the store outlives the panel, so a preview computed before this
 *  mount, or answered to an instance that is already gone, is on screen at first
 *  paint rather than a round trip later. */
export function usePendingPreview(): SyncPreview | null {
  return useSyncExternalStore(onPendingPreviewChange, getPendingPreviewSnapshot);
}

/** Hold a preview the backend has just handed back through `sync_preview`. True
 *  the moment it is written, so it outranks every read already open. */
export function adoptPreview(preview: SyncPreview): void {
  apply(++_issued, preview);
}

/** The user has answered the preview question — dismissed the card, started its
 *  apply, or pressed Sync for a fresh one. Also outranks every open read, which
 *  is what keeps a mount read from restoring a card they just answered. */
export function clearPendingPreview(): void {
  apply(++_issued, null);
}

/**
 * Ask the backend whether it is holding a preview for us, and hold it if so.
 *
 * Never rejects and never clears: a failed read is not an answer, and neither is
 * a `null` one (see the module docstring). The failure is logged here, so a
 * caller can fire this and forget it.
 */
export function refreshPendingPreview(): Promise<void> {
  const seq = ++_issued;
  return getPendingPreview().then(
    (answer) => {
      if (!answer.success || !answer.preview) return;
      apply(seq, answer.preview);
    },
    (e) => logError(`Failed to query pending preview: ${e}`),
  );
}

/** Reset the module state between tests. Not for production use. */
export function resetPendingPreviewStoreForTests(): void {
  _preview = null;
  _issued = 0;
  _applied = 0;
  _listeners.clear();
}
