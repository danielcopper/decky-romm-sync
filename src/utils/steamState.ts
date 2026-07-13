/**
 * Wrap MobX state mutations so Steam's observable stores allow direct writes.
 * On a strict-actions mobx build ``__mobxGlobals.allowStateChanges`` gates writes
 * to observables; flipping it true around the block (and restoring the prior
 * value) keeps direct overview mutations — e.g. the metadata-patch fields —
 * working there and on today's permissive build. A no-op when mobx globals
 * aren't present.
 */
export function stateTransaction<T>(block: () => T): T {
  // `typeof` guard so a bare global reference never throws where mobx isn't
  // injected (tests, or an early boot before Steam sets it up) — fall through to
  // running the block directly.
  const globals = typeof __mobxGlobals !== "undefined" ? __mobxGlobals : undefined;
  if (!globals) return block();
  const prev = globals.allowStateChanges;
  globals.allowStateChanges = true;
  try {
    return block();
  } finally {
    globals.allowStateChanges = prev;
  }
}
