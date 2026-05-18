/**
 * Tracks how many listeners are registered on `globalThis` for a given DOM event
 * name. Monkey-patches `globalThis.addEventListener` / `removeEventListener` to
 * count add/remove pairs. Use `installDomEventListenerSpy()` in `beforeEach` and
 * `uninstallDomEventListenerSpy()` in `afterEach` to keep the patch scoped to a
 * single test file.
 *
 * The spy passes the call through to the original `addEventListener` /
 * `removeEventListener` unchanged, so events still fire normally — the only
 * side effect is the per-name counter. Counting is exact: each `add` bumps the
 * counter by 1, each `remove` decrements it (never below zero). Use
 * `domListenerCount(name)` to read the current registration count for a single
 * event name, typically by capturing a baseline before render and comparing
 * after mount / after unmount to assert that a `useEffect` cleanup actually
 * removed its listener.
 *
 * `install` and `uninstall` are idempotent — calling either twice is a no-op
 * after the first effective call.
 */

type AnyListener = EventListenerOrEventListenerObject;

let originalAdd: typeof globalThis.addEventListener | null = null;
let originalRemove: typeof globalThis.removeEventListener | null = null;
const counts = new Map<string, number>();

/**
 * Install the spy on `globalThis`. Replaces `addEventListener` /
 * `removeEventListener` with wrappers that update an internal per-name counter
 * and then call through to the original implementations. Safe to call twice
 * (second call is a no-op). Pair with `uninstallDomEventListenerSpy()` in an
 * `afterEach` to restore the originals.
 */
export function installDomEventListenerSpy(): void {
  if (originalAdd !== null) return; // already installed
  originalAdd = globalThis.addEventListener.bind(globalThis);
  originalRemove = globalThis.removeEventListener.bind(globalThis);
  counts.clear();

  globalThis.addEventListener = ((
    name: string,
    listener: AnyListener,
    options?: boolean | AddEventListenerOptions,
  ) => {
    counts.set(name, (counts.get(name) ?? 0) + 1);
    originalAdd!(name, listener, options);
  }) as typeof globalThis.addEventListener;

  globalThis.removeEventListener = ((
    name: string,
    listener: AnyListener,
    options?: boolean | EventListenerOptions,
  ) => {
    const current = counts.get(name) ?? 0;
    if (current > 0) counts.set(name, current - 1);
    originalRemove!(name, listener, options);
  }) as typeof globalThis.removeEventListener;
}

/**
 * Restore the original `addEventListener` / `removeEventListener` and clear
 * the internal counter map. Safe to call twice (second call is a no-op).
 */
export function uninstallDomEventListenerSpy(): void {
  if (originalAdd === null) return; // not installed
  globalThis.addEventListener = originalAdd;
  globalThis.removeEventListener = originalRemove!;
  originalAdd = null;
  originalRemove = null;
  counts.clear();
}

/**
 * Return the count of listeners currently registered for `eventName` on
 * `globalThis`, as observed by the spy. Returns 0 if the spy is not installed
 * or no listeners have been registered for that name.
 */
export function domListenerCount(eventName: string): number {
  return counts.get(eventName) ?? 0;
}
