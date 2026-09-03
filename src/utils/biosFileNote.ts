/**
 * The note beside one BIOS file row — what the row IS, rather than a
 * download nobody has started.
 *
 * Two surfaces render a firmware row (the game detail panel's BIOS tab and the
 * System page) and they used to word the same facts separately, so a fact
 * gained on one surface was a fact missing on the other. This is the one place
 * that decides; each surface still frames the result its own way, because the
 * BIOS tab leaves plain absence to its status dot while the System page spells
 * it out in the row's description.
 *
 * Precedence is deliberate. A file the distribution itself put there is not a
 * gap in the RomM library — it is not the library's file at all, and telling
 * the user it is missing from RomM is true and useless — so that note wins.
 * A directory comes next, because neither "missing" nor "present" is the word
 * for a folder the reading did not look inside. Only then does the library
 * note stand, which is honest for every row nothing else was established for.
 */

import type { BiosFileStatus } from "../types";

/** The subset of a row this note is derived from — both surfaces' row shapes
 *  satisfy it, so neither has to be converted into the other's. */
export type BiosNoteRow = Pick<BiosFileStatus, "downloaded" | "on_server" | "supplied_by" | "is_directory">;

/**
 * The note for *row*, or `""` where the row's own state is the whole story.
 *
 * Returns `""` for a plain library file, present or missing alike: what the
 * surfaces disagree about is how to say "missing", so that word is left to
 * them (the System page appends its own; the tab's dot already carries it).
 */
export function biosFileNote(row: BiosNoteRow): string {
  if (row.supplied_by) return `provided by ${row.supplied_by}`;
  if (row.is_directory) return "a folder is here — its contents cannot be checked";
  if (row.on_server === false) {
    return row.downloaded ? "not in your RomM library" : "missing, not in your RomM library";
  }
  return "";
}
