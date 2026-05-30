/**
 * Fire-and-forget a promise whose rejection is intentionally ignored, without
 * the `void` operator (which SonarCloud S3735 bans and which leaves rejections
 * unhandled). The `.catch` attaches a rejection handler so no unhandledrejection
 * fires. For fire-and-forget work whose FAILURE SHOULD BE LOGGED, use an inline
 * `.catch((e) => logError(...))` at the call site instead of detach().
 */
export function detach(promise: Promise<unknown>): void {
  // Promise.resolve wraps the value so a rejection handler always attaches —
  // even when a caller (e.g. a test mock) hands us a non-thenable, which would
  // make a bare `promise.catch(...)` throw synchronously and defeat the
  // fire-and-forget contract. In production every caller passes a real Promise.
  Promise.resolve(promise).catch(() => undefined);
}
