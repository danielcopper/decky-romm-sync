/**
 * Asserts that a module store's `useSyncExternalStore` hook hands React ONE
 * stable subscribe function — the store's own exported `onXChange` seam — for
 * the life of a component, rather than a fresh closure per render.
 *
 * WHY THIS NEEDS A HELPER AT ALL. The property has no behavioral signature.
 * React tears the old subscription down before making the new one, so an
 * unstable subscribe leaks nothing and the store's live listener count is 1
 * either way; the whole cost is a re-subscribe on every render. Tidying
 *
 *     useSyncExternalStore(onMigrationChange, getMigrationState)
 *
 * into `useSyncExternalStore((cb) => onMigrationChange(cb), …)` is therefore
 * invisible: it was verified that the entire frontend suite stays green under
 * exactly that edit, applied to all four notice stores at once.
 *
 * WHY IT CANNOT BE OBSERVED FROM THE STORE'S SIDE. Two independent reasons:
 *   - each store's `_listeners` is module-private, so the number of live
 *     subscriptions is not readable from a test; and
 *   - the hook calls `onXChange` as a module-LOCAL binding, so a namespace spy
 *     (`vi.spyOn(store, "onXChange")`) does not intercept it. That is the same
 *     intra-module problem that forced `useMigrationStatus`'s unmount test to
 *     be rewritten when the hook moved next to its store: a spy on the module
 *     object only intercepts calls made from OTHER modules.
 *
 * What is left is the other side of the seam — what the hook passes to React —
 * and `useSyncExternalStore` is imported from "react", a different module, so
 * a module mock does reach it. `vi.spyOn(React, "useSyncExternalStore")` does
 * not work (the namespace is frozen: "Cannot redefine property"), so the
 * calling test file must install a CALL-THROUGH mock of react. It fakes
 * nothing — the real implementation runs, and the wrapper exists only to
 * record its arguments:
 *
 *     vi.mock("react", async (importOriginal) => {
 *       const actual = await importOriginal<typeof import("react")>();
 *       return { ...actual, useSyncExternalStore: vi.fn(actual.useSyncExternalStore) };
 *     });
 *
 * The `vi.mock` call is hoisted, so it has to sit in the test file itself and
 * cannot be moved in here. Everything after it can.
 */

import { expect, vi } from "vitest";
import { useSyncExternalStore } from "react";
import { renderHook } from "@testing-library/react";

/**
 * Render `useStoreHook`, re-render it twice, and assert that every
 * `useSyncExternalStore` call received the same subscribe function, and that it
 * is `subscribeSeam` — the store's own exported subscribe.
 *
 * Requires the calling test file to have installed the call-through react mock
 * described in this module's docstring; throws a pointed error if it has not,
 * rather than passing vacuously.
 */
export function expectStableSubscribe<T>(useStoreHook: () => T, subscribeSeam: (fn: () => void) => () => void): void {
  if (!vi.isMockFunction(useSyncExternalStore)) {
    throw new Error(
      "expectStableSubscribe needs the call-through react mock in the calling test file — " +
        "see src/test-utils/store-hook-subscription.ts.",
    );
  }
  const passed = vi.mocked(useSyncExternalStore);
  passed.mockClear();

  const { rerender, unmount } = renderHook(() => useStoreHook());
  rerender();
  rerender();

  const subscribes = passed.mock.calls.map(([subscribe]) => subscribe);
  // Guards the assertions below against a hook that stopped rendering at all,
  // which would otherwise satisfy both of them vacuously.
  expect(subscribes.length).toBeGreaterThan(1);
  expect(new Set(subscribes).size).toBe(1);
  expect(subscribes[0]).toBe(subscribeSeam);

  unmount();
}
