/**
 * Shared-account detection for the connection settings UI.
 *
 * The connection settings panel surfaces a warning when the configured
 * RomM username matches a well-known shared-account name (admin/guest/
 * root/...). Pure check — no React, no state, no I/O.
 */

/** Names that are commonly shared accounts on a self-hosted RomM server. */
export const SHARED_ACCOUNT_NAMES: ReadonlySet<string> = new Set([
  "admin",
  "romm",
  "user",
  "guest",
  "root",
]);

/**
 * Return true if the username matches a well-known shared-account name.
 * Comparison is case-insensitive and trims surrounding whitespace.
 */
export function isSharedAccount(username: string): boolean {
  return SHARED_ACCOUNT_NAMES.has(username.trim().toLowerCase());
}
