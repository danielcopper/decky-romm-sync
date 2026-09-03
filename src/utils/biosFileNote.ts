/**
 * The note beside one BIOS file row — what the row IS, rather than a
 * download nobody has started.
 *
 * Two surfaces render a firmware row (the game detail panel's BIOS tab and the
 * Library page's platform detail) and they used to word the same facts
 * separately, so a fact gained on one surface was a fact missing on the other.
 * This is the one place that decides; each surface still frames the result its
 * own way, because the BIOS tab leaves plain absence to its status dot while the
 * platform detail states it in the row's On-disk cell. What it hands over is a
 * sentence and a list of lines ({@link BiosFileWords}); where those go on the
 * page is each surface's own business, what they SAY is decided only here.
 *
 * Precedence is deliberate. A file the distribution itself put there is not a
 * gap in the RomM library — it is not the library's file at all, and telling
 * the user it is missing from RomM is true and useless — so that note wins.
 * What the reading established about the row comes next, because a folder's
 * contents and a destination something else occupies are neither "missing" nor
 * "present". Only then does the library note stand, which is honest for every
 * row nothing else was established for.
 *
 * The CAUSE of a verdict comes from the resolver's own caveat codes, because
 * `satisfied` is deliberately the verdict alone and carries none of it. What the
 * verdict does decide is which family of codes can apply — with the declaration
 * kind, since a folder's findings and a destination's are different families —
 * and what to say when no code in that family is recognised, which is the one
 * sentence written off the verdict itself.
 */

import type { BiosFileStatus } from "../types";

/** The subset of a row this note is derived from — both surfaces' row shapes
 *  satisfy it, so neither has to be converted into the other's. */
export type BiosNoteRow = Pick<
  BiosFileStatus,
  "downloaded" | "on_server" | "supplied_by" | "satisfied" | "declared_kind" | "caveats" | "images"
>;

/**
 * Everything a row says about itself: one sentence, and the lines under it.
 *
 * Both come back together because they are alternatives as often as they are
 * companions — a folder that lists what it holds needs no sentence saying it
 * holds something — and a surface taking one without the other would render a
 * satisfied folder as a bare name. What each surface still chooses is where the
 * lines go: the BIOS tab has an indented block under the row, the platform
 * detail puts them in its Contents column.
 */
export interface BiosFileWords {
  /** The em-dash note after the row's name, or `""` where there is none. */
  note: string;
  /** One line each, rendered under the row. Empty for every row but a folder
   *  whose read identified images, where the list IS the content. */
  lines: string[];
}

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
/** The destination could not be looked at at all — permissions, or failing
 *  storage. Emitted for either declaration kind, and worded only for a file:
 *  a folder's own unreadable-destination row is withheld and says so. */
const PATH_INACCESSIBLE = "firmware-path-inaccessible";

/**
 * What the reading established about *row*, or `""` where it has nothing to add.
 *
 * A satisfied folder lists what it holds, in the resolver's own words — those
 * are the core's own option labels, and the core needs exactly one of them, so
 * they carry no per-image required/optional marking and no mark for which one
 * will be loaded (that is a core option this plugin does not read).
 *
 * There is deliberately no branch for `firmware-search-unverified`, the code for
 * a folder whose candidates were never hashed. The backend asks the folder
 * question with verification on, so the resolver never emits it there; and the
 * unverified machine-wide reading emits it only for a row it leaves open, which
 * is exactly the row whose codes that reading does not carry. So it reaches no
 * row at all. A folder whose contents genuinely went unread lands on the
 * fallback below instead, and there are three ways to get there: the verified
 * read failed, the platform's scope never covered the core so nothing asked, or
 * the folder could not be looked at — the last arriving with
 * `firmware-path-inaccessible`, which is worded only on the file half. The
 * fallback claims nothing either way, which is the honest answer to all three.
 *
 * That asymmetry follows the verdicts rather than breaking from them. An
 * unreadable destination leaves a folder row withheld, where "its contents
 * could not be checked" already says the read is what failed; it leaves a file
 * row UNMET, where the surfaces would otherwise print their own word for
 * absence over a file nobody established the absence of. Both notes are
 * statements about the read, and neither claims the file is or is not there —
 * the verdict beside them says the requirement is not met, which is what
 * follows from a destination the emulator cannot open either.
 */
function verdictNote(row: BiosNoteRow): BiosFileWords {
  const has = (code: string) => (row.caveats ?? []).includes(code);
  if (row.declared_kind !== "directory") return fileAtItsDestination(has);
  if (row.satisfied === true) return folderHolding(row.images ?? []);
  if (row.satisfied === false) return folderUnmet(has);
  return folderWithheld(row.satisfied, has);
}

/** One line and nothing under it — every group but a satisfied folder's. */
const said = (note: string): BiosFileWords => ({ note, lines: [] });

/** A declared FILE: what the reading found at the place the emulator opens. */
function fileAtItsDestination(has: (code: string) => boolean): BiosFileWords {
  if (has(PATH_OBSTRUCTED)) return said("a folder is here, where the emulator opens a file");
  return said(has(PATH_INACCESSIBLE) ? "its location could not be read" : "");
}

/**
 * A folder whose read found an image — the one group whose content is a list.
 *
 * The images ARE the sentence here. "holds" above a list of three would be a
 * heading for a list that needs none, and folding them into the row's own name
 * is what pushed the status dot onto a line of its own.
 */
function folderHolding(images: string[]): BiosFileWords {
  return images.length > 0 ? { note: "", lines: [...images] } : said("holds a BIOS image");
}

/** A folder shown to hold nothing the core would boot, or none at all. */
function folderUnmet(has: (code: string) => boolean): BiosFileWords {
  if (has(PATH_NOT_A_DIRECTORY)) return said("a file is here, where the emulator opens a folder");
  return said(HOLDS_NO_IMAGE.some(has) ? "holds no BIOS image" : "");
}

/** A folder the read established nothing about, worded off why it could not. */
function folderWithheld(satisfied: boolean | null | undefined, has: (code: string) => boolean): BiosFileWords {
  if (has(IMAGE_CONTRADICTED)) return said("holds an image that could not be confirmed");
  if (READ_INCOMPLETE.some(has)) return said("its contents could not be read in full");
  return said(satisfied === null ? "its contents could not be checked" : "");
}

/**
 * What *row* says about itself — an empty note where its own state is the whole story.
 *
 * The note is `""` for a plain library file, present or missing alike: what the
 * surfaces disagree about is how to say "missing", so that word is left to
 * them (the platform detail spells it out; the tab's dot already carries it).
 */
export function biosFileNote(row: BiosNoteRow): BiosFileWords {
  if (row.supplied_by) return { note: `provided by ${row.supplied_by}`, lines: [] };
  const verdict = verdictNote(row);
  if (verdict.note || verdict.lines.length > 0) return verdict;
  // No RomM library holds a folder — the emulator lists that name — so the
  // library note would describe a download nobody can make.
  if (row.declared_kind === "directory") return { note: "", lines: [] };
  if (row.on_server === false) {
    return { note: row.downloaded ? "not in your RomM library" : "missing, not in your RomM library", lines: [] };
  }
  return { note: "", lines: [] };
}
