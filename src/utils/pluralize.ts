/**
 * Count-with-noun formatting for UI count lines. Turns a number and a singular
 * noun into "N noun" / "N nouns" with correct English pluralization (exactly 1
 * stays singular). Regular "+s" plural only — nouns with irregular plurals need
 * their own handling.
 */

/** ``count`` + a space + ``singular``, appending an ``s`` unless ``count`` is 1. */
export function pluralize(count: number, singular: string): string {
  return `${count} ${singular}${count === 1 ? "" : "s"}`;
}
