/**
 * Pure formatters and selectors for the SettingsPage UI. Anything that takes
 * inputs and returns outputs without touching component state or React
 * belongs here; rendering helpers live alongside their sections.
 */

/** Names that are commonly shared accounts on a self-hosted RomM server. */
export const SHARED_ACCOUNT_NAMES: ReadonlySet<string> = new Set([
  "admin",
  "romm",
  "user",
  "guest",
  "root",
]);

/** Format a relative time string (e.g. "5m ago", "2h ago") from an ISO string */
export function formatRelativeTime(isoStr: string | null): string {
  if (!isoStr) return "never";
  const date = new Date(isoStr);
  if (Number.isNaN(date.getTime())) return "unknown";
  const diffMs = Date.now() - date.getTime();
  const diffMin = Math.floor(diffMs / 60000);
  if (diffMin < 1) return "just now";
  if (diffMin < 60) return `${diffMin}m ago`;
  if (diffMin < 1440) return `${Math.floor(diffMin / 60)}h ago`;
  const d = date.getDate();
  const months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  return `${d} ${months[date.getMonth()]}`;
}

/** Format a one-line summary of the RetroArch save-sort flags. */
export function sortLabel(settings: { sort_by_content: boolean; sort_by_core: boolean }): string {
  return `Sort by content: ${settings.sort_by_content ? "ON" : "OFF"}, Sort by core: ${settings.sort_by_core ? "ON" : "OFF"}`;
}

/**
 * Return true if the username matches a well-known shared-account name.
 * Comparison is case-insensitive and trims surrounding whitespace.
 */
export function isSharedAccount(username: string): boolean {
  return SHARED_ACCOUNT_NAMES.has(username.trim().toLowerCase());
}
