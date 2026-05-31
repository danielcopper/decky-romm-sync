/**
 * In-memory mock of the `@decky/api` event bus for component tests.
 *
 * `@decky/api`'s `addEventListener` / `removeEventListener` route through the
 * Decky-loader WebSocket bridge at runtime — there is no DOM event here, so
 * tests cannot use `window.dispatchEvent`. Anything that wants to exercise a
 * component's `addEventListener(...)` wiring must dispatch through this
 * harness instead.
 *
 * Usage (the global mock factory in `src/test-setup.ts` already wires the
 * `@decky/api` module to this bus; tests just import the helpers):
 *
 *   import { emitDeckyEvent } from "../test-utils/decky-api-mock";
 *
 *   act(() => {
 *     emitDeckyEvent<[DownloadFailedEvent]>("download_failed", {
 *       rom_id: 1, rom_name: "X", platform_name: "PSX", error_message: "boom",
 *     });
 *   });
 *
 * Use `await act(async () => {...})` only if a listener under test is itself async.
 *
 * `resetDeckyEventBus()` is called automatically in the global `afterEach`,
 * so tests do not need to clean up listeners manually.
 *
 * This file mocks ONLY the Decky-loader event surface. DOM-level
 * `globalThis.dispatchEvent(new CustomEvent(...))` flows (`romm_data_changed`,
 * `romm_rom_uninstalled`, etc.) work natively in happy-dom and need no
 * harness.
 */

// Listeners are stored untyped — each test reifies the payload via the
// generic on `emitDeckyEvent` / `mockAddEventListener`.
type AnyListener = (...args: unknown[]) => unknown;

const listeners = new Map<string, Set<AnyListener>>();

/**
 * Stand-in for `@decky/api`'s `addEventListener`. Registers `listener` under
 * `name` and returns it unchanged — matching the upstream signature, which
 * returns the listener so callers can hand it straight to `removeEventListener`.
 */
export function mockAddEventListener<Args extends unknown[] = []>(
  name: string,
  listener: (...args: Args) => unknown,
): (...args: Args) => unknown {
  let bucket = listeners.get(name);
  if (!bucket) {
    bucket = new Set();
    listeners.set(name, bucket);
  }
  bucket.add(listener as AnyListener);
  return listener;
}

/**
 * Stand-in for `@decky/api`'s `removeEventListener`. No-op if the listener
 * was never registered (matches upstream tolerance).
 */
export function mockRemoveEventListener<Args extends unknown[] = []>(
  name: string,
  listener: (...args: Args) => unknown,
): void {
  const bucket = listeners.get(name);
  if (!bucket) return;
  bucket.delete(listener as AnyListener);
  if (bucket.size === 0) listeners.delete(name);
}

/**
 * Synchronously invokes every listener registered for `name` with `args`.
 * Use inside a React Testing Library `act(async () => { ... })` block when
 * the listener updates state so the resulting render flushes before assertions.
 *
 * Listener exceptions propagate — a test that wants to assert on error
 * handling should arrange for the listener to swallow its own throw.
 */
export function emitDeckyEvent<Args extends unknown[] = []>(name: string, ...args: Args): void {
  const bucket = listeners.get(name);
  if (!bucket) return;
  // Snapshot first so a listener that removes itself during dispatch doesn't
  // mutate the set we're iterating.
  for (const listener of [...bucket]) {
    listener(...args);
  }
}

/**
 * Drops every registered listener. Wired into the global `afterEach` in
 * `src/test-setup.ts`; tests that need a clean slate mid-test can call this
 * directly.
 */
export function resetDeckyEventBus(): void {
  listeners.clear();
}

/**
 * Test-only introspection — returns the count of listeners currently
 * registered under `name`. Useful for asserting that a component's
 * `useEffect` cleanup actually ran on unmount.
 */
export function deckyEventListenerCount(name: string): number {
  return listeners.get(name)?.size ?? 0;
}
