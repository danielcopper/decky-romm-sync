/**
 * Per-game core-switch reliability check.
 *
 * RetroDECK matches a gamelist.xml `<path>` against the ROM filename with an
 * awk *regex*, so the per-game `<altemulator>` core override is silently
 * ignored when the filename contains regex metacharacters. Clean names work;
 * names with `( ) [ ] { } + * ? | ^ $ \` do not. The dot `.` is intentionally
 * excluded — a regex dot matches the literal dot in `Tetris.gb`, so it is
 * harmless. This mirrors the upstream RetroDECK break condition tracked in
 * issue #210; we only surface it honestly.
 */

// Regex metacharacters that break RetroDECK's awk filename match. The dot is
// deliberately absent — it matches the literal dot and is harmless.
const CORE_SWITCH_BREAKING_CHARS = "()[]{}+*?|^$\\";

/**
 * Returns true iff `launchFileName` contains a character that breaks
 * RetroDECK's awk-regex filename match, meaning a per-game core override may be
 * silently ignored for this ROM.
 *
 * @param launchFileName - The ROM launch filename (basename) matched against
 *   the gamelist.xml `<path>` entry.
 */
export function coreSwitchMayBeIgnored(launchFileName: string): boolean {
  for (const ch of launchFileName) {
    if (CORE_SWITCH_BREAKING_CHARS.includes(ch)) return true;
  }
  return false;
}
