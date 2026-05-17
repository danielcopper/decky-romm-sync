/**
 * Event-target helpers for context-menu anchor resolution.
 *
 * `getEventTarget` replaces the `(e.currentTarget ?? e.target) as HTMLElement`
 * cast pattern at context-menu call sites — the cast is required for tsc
 * but flagged by SonarCloud's S4325 checker, which cannot resolve the same
 * type narrowing. Returns `HTMLElement | undefined` so callers can pass the
 * result directly to `showContextMenu`'s optional `EventTarget` parameter.
 */

export function getEventTarget(e: Event): HTMLElement | undefined {
  return (e.currentTarget ?? e.target) as HTMLElement | undefined;
}
