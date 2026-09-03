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
 * What the reading established about the row comes next, because a folder's
 * contents and a destination something else occupies are neither "missing" nor
 * "present". Only then does the library note stand, which is honest for every
 * row nothing else was established for.
 *
 * The wording keys off the resolver's own caveat CODES, never off the
 * declaration kind: what a row is and what was established about it are two
 * questions, and only the second has a sentence to write. `satisfied` decides
 * which family of codes to read, and is never worded on its own — it is the
 * verdict alone and carries none of its causes.
 */

import type { BiosFileStatus } from "../types";

/** The subset of a row this note is derived from — both surfaces' row shapes
 *  satisfy it, so neither has to be converted into the other's. */
export type BiosNoteRow = Pick<
  BiosFileStatus,
  "downloaded" | "on_server" | "supplied_by" | "satisfied" | "declared_kind" | "caveats" | "images"
>;

/** A directory is at a destination the emulator opens as a file, so nothing
 *  about what belongs there can be established — and it is not an absent file. */
const PATH_OBSTRUCTED = "firmware-path-obstructed";
/** A plain file sits where the core lists a folder: it has no inside, so the
 *  listing the core makes at load reaches nothing however right the file looks. */
const PATH_NOT_A_DIRECTORY = "firmware-path-not-a-directory";
/** The core's own test was made over every candidate and none passes it, or the
 *  folder holds no file of a size the core would even open. */
const HOLDS_NO_IMAGE = ["firmware-directory-holds-no-image", "firmware-directory-holds-no-candidate"];
/** The identity table names the bytes and the core's own header check denies
 *  them: two reads that disagree, and neither is taken over the other. */
const IMAGE_CONTRADICTED = "firmware-image-contradicted";
/** The folder could not be listed in full, or a candidate's bytes would not come
 *  back — a read failure, never a finding about what is in there. */
const READ_INCOMPLETE = ["firmware-scan-incomplete", "firmware-unreadable"];
/** Files of an accepted size are there and nothing was hashed: the content
 *  question was declined rather than answered. */
const SEARCH_UNVERIFIED = "firmware-search-unverified";

/**
 * What the reading established about *row*, or `""` where it has nothing to add.
 *
 * A satisfied folder lists what it holds, in the resolver's own words — those
 * are the core's own option labels, and the core needs exactly one of them, so
 * they carry no per-image required/optional marking and no mark for which one
 * will be loaded (that is a core option this plugin does not read).
 */
function verdictNote(row: BiosNoteRow): string {
  const caveats = row.caveats ?? [];
  const has = (code: string) => caveats.includes(code);
  if (row.declared_kind !== "directory") {
    return has(PATH_OBSTRUCTED) ? "a folder is here, where the emulator opens a file" : "";
  }
  if (row.satisfied === true) {
    const images = row.images ?? [];
    return images.length > 0 ? `holds ${images.join(", ")}` : "holds a BIOS image";
  }
  if (row.satisfied === false) {
    if (has(PATH_NOT_A_DIRECTORY)) return "a file is here, where the emulator opens a folder";
    return HOLDS_NO_IMAGE.some(has) ? "holds no BIOS image" : "";
  }
  if (has(IMAGE_CONTRADICTED)) return "holds an image that could not be confirmed";
  if (READ_INCOMPLETE.some(has)) return "its contents could not be read in full";
  if (has(SEARCH_UNVERIFIED)) return "its contents were not checked";
  return row.satisfied === null ? "its contents could not be checked" : "";
}

/**
 * The note for *row*, or `""` where the row's own state is the whole story.
 *
 * Returns `""` for a plain library file, present or missing alike: what the
 * surfaces disagree about is how to say "missing", so that word is left to
 * them (the System page appends its own; the tab's dot already carries it).
 */
export function biosFileNote(row: BiosNoteRow): string {
  if (row.supplied_by) return `provided by ${row.supplied_by}`;
  const verdict = verdictNote(row);
  if (verdict) return verdict;
  // No RomM library holds a folder — the emulator lists that name — so the
  // library note would describe a download nobody can make.
  if (row.declared_kind === "directory") return "";
  if (row.on_server === false) {
    return row.downloaded ? "not in your RomM library" : "missing, not in your RomM library";
  }
  return "";
}
