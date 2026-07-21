/**
 * Subsequence fuzzy match (fzf-style): every character of `query` must appear in
 * `target` in order, case-insensitively. Used to filter long name lists (the
 * Danger Zone whitelist, the Collections list) from a single search box.
 *
 * An empty query matches everything, so an empty search box never hides rows.
 */
export function fuzzyMatch(query: string, target: string): boolean {
  const q = query.toLowerCase();
  const t = target.toLowerCase();
  let qi = 0;
  for (let ti = 0; ti < t.length && qi < q.length; ti++) {
    if (t[ti] === q[qi]) qi++;
  }
  return qi === q.length;
}
