export function formatTimestamp(iso: string | null): string {
  if (!iso) return "unknown";
  try {
    const d = new Date(iso);
    return d.toLocaleString(undefined, {
      month: "short",
      day: "numeric",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
    });
  } catch {
    return iso;
  }
}

/**
 * Format an ISO-8601 timestamp as a coarse "Xm ago" label, recomputed at call time.
 * Mirrors what the backend used to emit but stays fresh between fetches —
 * the backend now ships only the raw ISO timestamp.
 *
 * Returns `null` when the input cannot be parsed; callers decide the fallback label.
 */
export function formatTimeAgo(iso: string): string | null {
  const ms = Date.parse(iso);
  if (Number.isNaN(ms)) return null;
  const diffMin = Math.floor((Date.now() - ms) / 60000);
  if (diffMin < 1) return "Just now";
  if (diffMin < 60) return `${diffMin}m ago`;
  if (diffMin < 1440) return `${Math.floor(diffMin / 60)}h ago`;
  return `${Math.floor(diffMin / 1440)}d ago`;
}
