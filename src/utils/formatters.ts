/** Format a Unix timestamp (seconds) as a coarse human-readable date.
 *  Returns "Never" for zero/negative, "Today"/"Yesterday"/"Xd ago" for recent,
 *  and "DD. Mon." (or "DD. Mon. YYYY" if not the current year) for older. */
export function formatLastPlayed(timestamp: number): string {
  if (!timestamp || timestamp <= 0) return "Never";
  const date = new Date(timestamp * 1000);
  const now = new Date();
  const diffMs = now.getTime() - date.getTime();
  const diffDays = Math.floor(diffMs / (1000 * 60 * 60 * 24));

  if (diffDays === 0) return "Today";
  if (diffDays === 1) return "Yesterday";
  if (diffDays < 7) return `${diffDays} days ago`;

  const months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  const day = date.getDate();
  const month = months[date.getMonth()];
  const year = date.getFullYear();
  if (year === now.getFullYear()) return `${day}. ${month}.`;
  return `${day}. ${month}. ${year}`;
}

/** Format a duration in minutes as a compact playtime string. */
export function formatPlaytime(minutes: number): string {
  if (!minutes || minutes <= 0) return "None";
  if (minutes < 60) return `${minutes} Min`;
  const hours = Math.floor(minutes / 60);
  const remainingMin = minutes % 60;
  if (remainingMin === 0) return hours === 1 ? "1 Hour" : `${hours} Hours`;
  return `${hours}h ${remainingMin}m`;
}

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
