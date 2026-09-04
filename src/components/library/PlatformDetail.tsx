/**
 * Everything about one platform, in the Platforms tab's right-hand pane: what
 * it holds, which core launches it, the BIOS files that core wants, and the two
 * ways to take it back out of Steam.
 *
 * The pane scrolls by moving focus, so every row a reader must reach is a focus
 * stop — the BIOS table's rows included, which is why a row with nothing to
 * press still carries an activate handler.
 *
 * The sync toggle is deliberately absent: it lives in the list row, where the
 * focus already is.
 *
 * Structure and vocabulary: `docs/architecture/qam-panel.md`, section Library.
 */

import type { FC, ReactNode } from "react";
import { ConfirmModal, DialogButton, Focusable, showContextMenu, showModal, Spinner } from "@decky/ui";
import { FaMicrochip } from "react-icons/fa";
import type { FirmwarePlatformExt, SystemCoreInfo } from "../../types";
import { biosColorForLevel } from "../../utils/biosColor";
import { biosFileNote } from "../../utils/biosFileNote";
import { buildEmulatorMenu } from "../../utils/emulatorMenu";
import { getEventTarget } from "../../utils/events";
import { SYNC_RUNNING_HINT, useSyncRunning } from "../../utils/syncRunning";
import type { CoreAnswer, PlatformRow, PlatformsPageState, StatusScope } from "./usePlatformsPage";

/** The size every secondary LINE on this pane is set in: the header's counts
 *  clause, the under-row description and note lines, the Contents cell beside
 *  them, the muted sentences, the table header and the legend. One constant,
 *  because the device pass asked for the cell to match those lines and a second
 *  literal is how they drift apart again. Button labels are not lines and keep
 *  their own sizes. */
const SECONDARY_FONT = "11px";

const MUTED = "#8f98a0";
const RED = "#d94126";
const GREEN = "#5ba32b";
const AMBER = "#d4a72c";
/** A satisfied row nothing is waiting on — present, and not this core's problem.
 *  Green's quieter twin, so "there and needed" and "there and spare" are one
 *  glance apart rather than one reading apart. */
const PALE_GREEN = "#8fc46b";
/** The second mark's own colour, deliberately outside the verdict palette's
 *  traffic light: what it reports is not a degree of wrongness. */
const VIOLET = "#a48fd4";

/**
 * The second mark: your RomM library does not hold this file.
 *
 * It sits BESIDE the verdict mark and never in place of it — the two are
 * different questions, and a row that is present but unfetchable ("you have it,
 * but you could not fetch it again") is exactly the combination a single
 * channel would lose. Which is why it also carries no need axis of its own: the
 * mark it stands next to already says whether the file is wanted.
 *
 * `⊘` is not one of the verdict shapes and reads as "not available" rather than
 * as a degree of wrong; DejaVu Sans carries U+2298, the same fontconfig
 * fallback the `✓`/`✗` glyphs already rely on for this table.
 */
const LIBRARY_MARK = { glyph: "⊘", color: VIOLET, title: "not in your RomM library" } as const;

/**
 * The padding a `DialogButton` is given wherever this pane puts buttons in a
 * row.
 *
 * `ButtonItem` — the full-width control most of the panel uses — takes no style
 * or class of its own: its props are `ItemProps`, which has neither
 * (`@decky/ui/dist/components/Item.d.ts`), so its height is Steam's and cannot
 * be argued with from here. `DialogButton` does take `style`
 * (`DialogButtonProps extends DialogCommonProps`, `Dialog.d.ts`), and is Steam's
 * own button component rather than a lookalike of one.
 *
 * They are the same button, and the difference is the row around it. In
 * `chunk~2dcc5aaf7.js` module 12316, the `forwardRef` decky's prop-list regex
 * matches (`highlightOnFocus` then `childrenContainerWidth`) renders a `Field`
 * whose first child is a second `forwardRef`, and that one renders `o.$n`;
 * module 64608 re-exports `$n` from module 44351, where it is the `forwardRef`
 * whose className is `"DialogButton","_DialogLayout","Secondary"` — the exact
 * string `@decky/ui` searches for to bind its own `DialogButton`
 * (`components/Dialog.js`, `DialogButton = DialogButtonSecondary`). So
 * `ButtonItem` IS a `Field` wrapped around this component, and what a `Field`
 * costs is the row's own padding: 10px top and bottom inside the QAM, where it
 * renders in its `Classic` mode.
 */
const FLAT_BUTTON = { flex: "1 1 auto", minWidth: 0, padding: "6px 10px", fontSize: "13px" } as const;

/** The button in a BIOS row's action column. Narrow because the column is
 *  sized for it and the file name beside it is the thing worth width: 4px of
 *  horizontal padding on a 92px column still leaves a target wider than it is
 *  tall, which is what keeps it pressable at the Deck's scale. */
const ROW_BUTTON = { width: "100%", minWidth: 0, padding: "4px", fontSize: "11px" } as const;

/**
 * The core picker's button in the header line — the game page's icon button, at
 * this line's scale.
 *
 * The chrome is written out rather than reusing `.romm-gear-btn`, for a reason
 * that needs no claim about which document the class reaches: that class is
 * 36×36 (`styleInjector.ts`), which is the game page's play row, and this line
 * is 28px tall. What IS shared is what the user asked to be shared — the icon
 * and its two colours.
 */
const CORE_BUTTON = {
  alignSelf: "center",
  display: "flex",
  alignItems: "center",
  justifyContent: "center",
  width: "28px",
  minWidth: "28px",
  height: "28px",
  padding: 0,
  borderRadius: "4px",
  flexShrink: 0,
} as const;

/** One row of the firmware overview's per-platform file list. Named off the
 *  payload rather than restated, so a field added to it reaches here. */
type FirmwareRow = FirmwarePlatformExt["files"][number];

/**
 * Build the per-platform summary label/description from the backend BIOS
 * aggregates. The ok/partial/missing DECISION is the backend's `bios_level`
 * (`compute_bios_level`) — `requiredReady` is `bios_level === "ok"`, so the
 * required-files threshold is no longer re-compared here. `requiredCount` still
 * selects the phrasing axis (required vs. plain file counts), and the
 * optional-missing breakdown stays a local computation passed in by the caller.
 *
 * With nothing required, the library ratio is inventory and is worded as such —
 * the same framing the BIOS tab uses. "0 / 20 files … 20 missing" over twenty
 * files no installed core asks for reads as work outstanding on a system that
 * needs nothing.
 */
function getBiosSummary(
  requiredCount: number,
  requiredDone: number,
  requiredReady: boolean,
  optionalMissing: number,
  done: number,
  total: number,
) {
  if (requiredCount > 0 && requiredReady) {
    return {
      summaryLabel: `${requiredDone} / ${requiredCount} required`,
      summaryDescription:
        optionalMissing > 0 ? `All required ready (${optionalMissing} optional missing)` : "All required ready",
    };
  }
  if (requiredCount > 0) {
    return {
      summaryLabel: `${requiredDone} / ${requiredCount} required`,
      summaryDescription: `${requiredCount - requiredDone} required missing`,
    };
  }
  return {
    summaryLabel: "Nothing required",
    summaryDescription: total > 0 ? `${done} / ${total} files held` : "No BIOS files in your library",
  };
}

/**
 * The summary for a platform making no readiness claim. Two shapes reach it and
 * they are different sentences.
 *
 * `requiredWithheld` above zero is a platform whose emulators DID answer and one
 * of whose required rows nothing could judge — a declared folder the resolver
 * could not read, say. Zero is no installed emulator's answer being established
 * for the platform at all.
 *
 * The second states no count. The rows nothing could answer for are counted
 * once, under the table where the line that carries them also says where to
 * report the gap — and on this platform they are every row, so a count up here
 * as well is the same sentence twice on one screen.
 */
function getUnknownSummary(requiredWithheld: number) {
  if (requiredWithheld > 0) {
    return {
      summaryLabel: "BIOS readiness unknown",
      summaryDescription:
        requiredWithheld === 1
          ? "A required file could not be judged — see the file list"
          : `${requiredWithheld} required files could not be judged — see the file list`,
    };
  }
  return {
    summaryLabel: "BIOS requirement unknown",
    summaryDescription: "Nothing installed could answer for this system",
  };
}

/**
 * The first mark in one row's `On disk` cell, as a glyph and a colour.
 *
 * Two facts, two channels, so neither has to be read off the other:
 *
 * - **the glyph is the VERDICT** — `✓` met, `✗` not met, `?` nothing could
 *   establish it. It is `BiosFileEntry.satisfied`, **never presence**: for a
 *   declared folder the two come apart completely, since RetroDECK links
 *   LRPS2's `pcsx2/bios` onto the BIOS root, so the folder is always there and
 *   what satisfies the core is a file inside it.
 * - **the colour is the NEED** — strong where the core the platform launches
 *   with requires the file, muted where it does not, **amber where nothing
 *   could say**. Keyed on `required_by_active`, not `wanted`, because the
 *   summary above the table counts the same way and the two must not disagree
 *   about what "required" means.
 *
 * The two axes are read in that order, and the order is load-bearing. A row with
 * no placement is `wanted: "unknown"` — no installed emulator could be asked —
 * and its verdict is `downloaded` all the same (`domain/bios_status.py`,
 * `_row_verdict(None, …)`), so it IS established. Testing the need axis first
 * would spend the glyph on a need-axis fact and throw that verdict away, and a
 * platform nothing could be asked about is made entirely of such rows: that is
 * the pane telling the reader they can place BIOS files by hand, so which ones
 * are already there is the one thing it must not stop saying.
 *
 * So the four states the device pass asked for come out as required + met green
 * `✓`, required + unmet red `✗`, spare + met pale green `✓`, spare + unmet grey
 * `✗`; a need nothing could establish keeps its glyph and goes amber; and only a
 * verdict nothing could establish becomes `?`.
 *
 * `not_needed` and `optional` share the muted branch on purpose: for the core
 * about to launch, a file it does not require is not a gap either way.
 *
 * The `Contents` cell reads the same `satisfied`, so the two columns are two
 * renderings of one field and cannot contradict each other.
 */
function diskMark(file: FirmwareRow): { glyph: string; color: string; title: string } {
  // A folder's verdict is what it HOLDS, never that the folder is there — the
  // register's rule — so a payload carrying no verdict for one leaves the row
  // unestablished rather than falling back to presence. For a declared file
  // `downloaded` IS the verdict, which is the only thing the fallback is for.
  const declaredFolder = file.declared_kind === "directory";
  const verdict = file.satisfied !== undefined ? file.satisfied : declaredFolder ? null : file.downloaded;
  if (verdict === null) return { glyph: "?", color: AMBER, title: "nothing could check this" };

  const needUnknown = file.wanted === "unknown";
  const required = file.required_by_active;
  if (verdict) {
    if (needUnknown) return { glyph: "✓", color: AMBER, title: "here; nothing could say whether it is wanted" };
    return { glyph: "✓", color: required ? GREEN : PALE_GREEN, title: required ? "required, here" : "here" };
  }
  if (needUnknown) return { glyph: "✗", color: AMBER, title: "missing; nothing could say whether it is wanted" };
  return { glyph: "✗", color: required ? RED : MUTED, title: required ? "required, missing" : "missing" };
}

/**
 * The row's second mark, or `null` where the library holds the file.
 *
 * One field, one rule, no conditionality on the verdict: a file downloaded
 * before it left the library is still one that cannot be fetched again, so the
 * mark appears beside a `✓` exactly as it does beside a `✗`.
 *
 * `on_server` absent is not `false` — it is a payload that never spoke about
 * the library — so an unstated row carries no mark rather than a claim.
 *
 * A declared FOLDER is out, and not as a special case: no RomM library holds a
 * folder, so `_overview_row` stamps every folder row `on_server: False`
 * unconditionally and the mark would say "your library does not hold this"
 * about something no library can. That is the sentence `biosFileNote` already
 * refuses to produce for the same reason, and the same reason keeps folder rows
 * out of the download filter and out of the download batch.
 */
function libraryMark(file: FirmwareRow): typeof LIBRARY_MARK | null {
  return file.on_server === false && file.declared_kind !== "directory" ? LIBRARY_MARK : null;
}

const SectionTitle: FC<{ title: string; note?: string; noteColor?: string }> = ({ title, note, noteColor }) => (
  <div style={{ display: "flex", alignItems: "baseline", gap: "8px", padding: "12px 16px 4px" }}>
    <span style={{ fontSize: "12px", fontWeight: 600, letterSpacing: "0.5px", color: "#dcdedf" }}>
      {title.toUpperCase()}
    </span>
    {note && <span style={{ fontSize: SECONDARY_FONT, color: noteColor ?? MUTED }}>{note}</span>}
  </div>
);

const Muted: FC<{ children: ReactNode }> = ({ children }) => (
  <div style={{ fontSize: SECONDARY_FONT, color: MUTED, padding: "0 16px 6px" }}>{children}</div>
);

/** The status line for one group of one platform, or nothing. Both halves
 *  matter: a failed core switch must not be reported under Remove, and an
 *  action's result must not follow the reader onto the next platform's pane. */
const GroupStatus: FC<{ state: PlatformsPageState; slug: string; scope: StatusScope }> = ({ state, slug, scope }) =>
  state.status && state.status.slug === slug && state.status.scope === scope ? (
    <div data-testid={`status-${scope}`} style={{ fontSize: "12px", color: "#dcdedf", padding: "0 16px 8px" }}>
      {state.status.text}
    </div>
  ) : null;

/**
 * Why this pane's buttons are dead while nothing on it is running.
 *
 * An action disables every platform's actions, not just its own, because the
 * page holds one `status`, one `removalProgress` and one `busySlug` — a second
 * action would clobber the first's line and the first `finally` would clear the
 * busy state under the second. The line that would explain the wait —
 * {@link GroupStatus} — is bound to the platform the action belongs to, so
 * walking away from a running removal leaves a pane full of disabled buttons and
 * nothing said. This is what it says.
 */
const BusyElsewhere: FC<{ row: PlatformRow; state: PlatformsPageState }> = ({ row, state }) => {
  if (state.busySlug === null || state.busySlug === row.slug) return null;
  const other = state.rows.get(state.busySlug)?.name ?? "another platform";
  return <Muted>{`Working on ${other} — actions here are paused until it finishes.`}</Muted>;
};

// File, On disk, Contents, and the row's own button. Every column but the first
// is sized for the widest thing it can hold, measured on the device at the
// Deck's scale: the `On disk` heading is 38.7px and two marks are ~34px; the
// widest Contents value ("12 images") is 76.8px against a 46.1px heading; the
// Download button's label is ~55px. What that leaves goes to the file name,
// which is the column with something to say. None of them is conditional — a
// column that appears and disappears per platform is worse than a narrow one,
// and `Contents` is about to be filled for file rows (#1803).
const TABLE_COLUMNS = "1fr 48px 84px 92px";

const BiosTableHeader: FC = () => (
  // Column names accompany the rows below them and scroll with them; making the
  // header a focus stop would add a step that leads nowhere.
  <div
    style={{
      display: "grid",
      gridTemplateColumns: TABLE_COLUMNS,
      gap: "8px",
      padding: "0 16px 4px",
      fontSize: SECONDARY_FONT,
      color: MUTED,
    }}
  >
    <span>File</span>
    <span>On disk</span>
    <span>Contents</span>
    <span />
  </div>
);

/**
 * The Contents cell — what a read of the row's destination found inside it.
 *
 * The em dash means **nothing was asked**, and must never come to mean "asked
 * and found nothing": the whole-machine inventory is deliberately unverified, so
 * a plain file row carries no content answer at all and #1803 is the work that
 * will give it one. A declared folder does carry one, because the resolver lists
 * that folder the way the core does, and the row's `satisfied` verdict is what
 * came back.
 *
 * "no image" covers both ways a folder fails its core — one holding nothing the
 * core would boot, and a plain file sitting where the core opens a folder —
 * because the core's listing reaches no image either way. Which of the two it is
 * belongs to the line under the row, whose wording is {@link biosFileNote}'s.
 */
function contentsCell(file: FirmwareRow): string {
  if (file.declared_kind !== "directory") return "—";
  if (file.satisfied === true) {
    const held = file.images?.length ?? 0;
    if (held === 0) return "an image";
    return held === 1 ? "1 image" : `${held} images`;
  }
  return file.satisfied === false ? "no image" : "unknown";
}

/**
 * What a row says under itself: its note, and the images a folder holds.
 *
 * Full width, because the alternative is a 48px cell wrapping one sentence
 * across three lines — the cost the marks were introduced to stop paying. A
 * note here is also rare in the ordinary case: over the `.info` corpus the rows
 * that carry one are the handful RetroDECK supplies itself and PS2's folder
 * row. That is a statement about a healthy install, not about the vocabulary —
 * `biosFileNote`'s caveat wording appears wherever a destination cannot be
 * read, which no corpus can predict.
 *
 * Each image string is the resolver's verbatim, and `pre-wrap` keeps the column
 * padding PCSX2 puts in its own option labels — that alignment is what makes a
 * line matchable against the emulator's own picker. They sit under the row
 * rather than in the Contents cell because the cell is 84px and one of these
 * labels is not; the cell counts them instead.
 */
const BiosRowLines: FC<{ lines: string[] }> = ({ lines }) =>
  lines.length === 0 ? null : (
    <div style={{ display: "flex", flexDirection: "column", gap: "2px", marginLeft: "18px", marginTop: "2px" }}>
      {lines.map((line) => (
        <div key={line} style={{ fontSize: SECONDARY_FONT, color: MUTED, whiteSpace: "pre-wrap" }}>
          {line}
        </div>
      ))}
    </div>
  );

/**
 * The description beside a file's name, with the name itself taken back out.
 *
 * **It is not RomM's description** — `_group_server_firmware` builds no
 * `description` key at all, and `_wanted_fields` overwrites whatever came in.
 * What arrives is the core's own `firmwareN_desc` out of its `.info` file, or,
 * for a row no placement covers, the file name itself (`build_file_entry`'s
 * `else file_name`). Both spell the name into the words.
 *
 * Measured over the 292 `.info` files a stock RetroDECK ships — 695 declared
 * firmware entries — the description's relation to the row's own `file_name`
 * (which is `os.path.basename` of the declared path) falls into six shapes:
 *
 * | 245 | 35% | it IS the name — `"macventure.dat"`                          |
 * | 328 | 47% | the name, a space, then prose — `"scph5500.bin (PS1 JP BIOS)"` |
 * | 115 | 17% | the same, but the name carries its directory — `"dc/dc_boot.bin (Dreamcast BIOS)"` |
 * |   5 |  1% | the first token names something else — a folder the file sits in (`"'Databases' folder"`), or a misspelling of it (two upstream typos) |
 * |   1 |  0% | it names the file, but the name has a space in it — `"7800 BIOS (U).rom (7800 BIOS)"` |
 * |   1 |  0% | it names the file in quotes — `"'pcsx2/bios' folder"`, the corpus's only folder declaration |
 *
 * So the rule has two halves: strip the name where the description opens with
 * it verbatim (which is the only way a name containing spaces can be seen), and
 * otherwise strip a first token that names this file — as itself or at the end
 * of a path, with surrounding quotes ignored.
 * Together they fire on 690 of the 695 and on the no-placement case; the
 * remaining five say something real and are printed whole. The name half is
 * anchored at the start rather than searched for anywhere, because a rule that
 * scanned the whole string would cut into prose that merely quotes the name.
 * The prose is kept verbatim, parentheses and all, because it is the packager's
 * own words and re-punctuating it is a second way to be wrong.
 *
 * The counts were taken over the deployed flatpak with
 * `grep -o … | wc -l`-style matching per entry rather than per line: the shapes
 * are counted by classifying every `firmwareN_path` / `firmwareN_desc` pair,
 * which is reproducible by re-running that classification over the same tree.
 */
function fileDescription(file: FirmwareRow): string | null {
  // A declared FOLDER shows none. Its meaning is its verdict and the images
  // listed under it — LRPS2 never reads a file name, so what the row says is
  // "this folder holds something the core will boot", which `✓` and the image
  // lines already say. The corpus's one folder is described as
  // `'pcsx2/bios' folder`, which after the name comes out leaves the bare word
  // "folder": a restatement of `declared_kind`. This is a rule about what a
  // folder ROW shows, not a prediction about what descriptions exist.
  if (file.declared_kind === "directory") return null;
  const description = file.description.trim();
  if (!description) return null;
  // A name with a space in it is not one token, so the token rule cannot see it.
  // Exactly one of the 695 is spelled that way ("7800 BIOS (U).rom"), and it
  // printed the name twice until this line. Anchored at the start rather than
  // searched for anywhere, so prose that merely quotes the name is left alone.
  if (description.startsWith(`${file.file_name} `)) {
    return description.slice(file.file_name.length).trim() || null;
  }
  const [head, ...tail] = description.split(" ");
  // Quotes are stripped before the comparison, because the corpus's one folder
  // declaration is described as `'pcsx2/bios' folder` — a token that names the
  // declaration exactly, which the row's own name line is already showing, and
  // which nothing else would have removed. Comparing the whole declared path as
  // well would change no outcome: `file_name` is its basename, so a token
  // equalling the path always equals the basename after the split too.
  const token = (head ?? "").replace(/^['"]|['"]$/g, "");
  if ((token.split("/").pop() ?? "") !== file.file_name) return description;
  const rest = tail.join(" ").trim();
  return rest || null;
}

/**
 * The folder the emulator declared this file in, with its trailing slash, or
 * `null` for a file that belongs at the root of the BIOS directory.
 *
 * The row's own name is a basename, so without this the pane cannot answer the
 * one question a user placing a file by hand has: which folder. 207 of the 695
 * declarations a stock RetroDECK ships name a subdirectory — `dc/dc_boot.bin`,
 * `ep128emu/roms/exos21.rom` — and their descriptions spell it in only 115 of
 * those, so the description is not a substitute for the declaration.
 */
function declaredFolder(file: FirmwareRow): string | null {
  const declared = file.declared_path;
  if (declared === undefined || !declared.includes("/")) return null;
  return `${declared.slice(0, declared.lastIndexOf("/"))}/`;
}

/**
 * What the marks in `On disk` mean, under the table that uses them.
 *
 * One entry per line, which is what makes a two-mark cell readable and is what
 * keeps the filter doing real work: only the entries the table actually
 * contains, because a line for a state no row is in explains nothing and costs
 * a row, the scarce thing on this pane. Mark 2 is inside that filter as well —
 * a platform whose library holds every file shows no line for it — and gets one
 * line rather than one per pairing, since it means the same beside every
 * verdict. The order is the order a reader cares about: what is wrong first,
 * then the second channel.
 */
const BiosLegend: FC<{ files: FirmwareRow[] }> = ({ files }) => {
  const marks = files.map(diskMark);
  const shown = [
    { glyph: "✗", color: RED, text: "required, missing" },
    { glyph: "✓", color: GREEN, text: "required, here" },
    { glyph: "✗", color: AMBER, text: "missing; nothing could say whether this is wanted" },
    { glyph: "✓", color: AMBER, text: "here; nothing could say whether this is wanted" },
    { glyph: "?", color: AMBER, text: "could not be checked" },
    { glyph: "✓", color: PALE_GREEN, text: "here, not required" },
    { glyph: "✗", color: MUTED, text: "missing, not required" },
  ].filter((entry) => marks.some((mark) => mark.glyph === entry.glyph && mark.color === entry.color));
  if (files.some((file) => libraryMark(file) !== null)) {
    shown.push({ glyph: LIBRARY_MARK.glyph, color: LIBRARY_MARK.color, text: LIBRARY_MARK.title });
  }
  if (shown.length === 0) return null;
  return (
    <div
      data-testid="bios-legend"
      style={{
        display: "flex",
        flexDirection: "column",
        padding: "2px 16px 6px",
        fontSize: SECONDARY_FONT,
        lineHeight: 1.3,
      }}
    >
      {shown.map((entry) => (
        <span key={`${entry.glyph}${entry.color}`} style={{ color: MUTED }}>
          <span style={{ color: entry.color }}>{entry.glyph}</span> {entry.text}
        </span>
      ))}
    </div>
  );
};

const BiosFileRow: FC<{ file: FirmwareRow; action: ReactNode }> = ({ file, action }) => {
  const { note, lines, fromLibrary } = biosFileNote(file);
  const mark = diskMark(file);
  const library = libraryMark(file);
  const description = fileDescription(file);
  const folder = declaredFolder(file);
  // The library note is the one sentence the cell's second mark now carries, and
  // on a platform whose library holds little it was the same words under nearly
  // every row. Everything else moves under the row rather than into the cell.
  const rowLines = fromLibrary ? [] : [...(note ? [note] : []), ...lines];
  const cells = (
    <>
      <div style={{ display: "grid", gridTemplateColumns: TABLE_COLUMNS, gap: "8px", alignItems: "center" }}>
        {/* The cell ellipsises, and with the folder in front of it what gets cut
            is now the NAME rather than the description that used to sit here —
            `scummvm/extra/hadesch_translations.dat` does not fit 202px in any
            arrangement. The title is the mouse's way back to it; a reader on the
            controller has none, and the only real fix is width the list column
            currently holds. */}
        <span
          title={file.declared_path ?? file.file_name}
          style={{ minWidth: 0, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}
        >
          {folder && <span style={{ color: MUTED }}>{folder}</span>}
          {file.file_name}
        </span>
        <span style={{ display: "flex", gap: "4px", fontSize: "14px", whiteSpace: "nowrap" }}>
          <span data-testid="disk-mark" style={{ color: mark.color }} title={mark.title}>
            {mark.glyph}
          </span>
          {library && (
            <span data-testid="library-mark" style={{ color: library.color }} title={library.title}>
              {library.glyph}
            </span>
          )}
        </span>
        <span style={{ color: MUTED, fontSize: SECONDARY_FONT }}>{contentsCell(file)}</span>
        <span>{action}</span>
      </div>
      {description && (
        <div
          style={{
            marginLeft: "18px",
            fontSize: SECONDARY_FONT,
            color: MUTED,
            overflow: "hidden",
            textOverflow: "ellipsis",
            whiteSpace: "nowrap",
          }}
        >
          {description}
        </div>
      )}
      <BiosRowLines lines={rowLines} />
    </>
  );
  if (action) return <div style={{ padding: "4px 16px" }}>{cells}</div>;
  // A row with nothing to press still has to be reachable, or the reader cannot
  // scroll past it to the rows below: the activate handler is what makes a
  // Focusable a focus stop. `action` is therefore a NODE that may be null and
  // never a component element — an element is always truthy, which is how this
  // branch went dead once while this comment went on explaining it.
  return (
    <Focusable onActivate={() => {}} style={{ padding: "4px 16px" }}>
      {cells}
    </Focusable>
  );
};

/**
 * What the pane can offer for this platform's core: a pick, or a sentence
 * saying why there is nothing to pick.
 *
 * One decision, because the answer is rendered in two places — the header's
 * icon button and the line under it — and splitting it would let the two
 * disagree, which is a sentence saying there is nothing to switch under a
 * button that switches.
 */
type CoreOffer = { kind: "pick"; core: SystemCoreInfo } | { kind: "say"; text: string };

function coreOffer(row: PlatformRow, core: CoreAnswer): CoreOffer {
  // Strictly zero, so an unread shortcut count does not withdraw the picker:
  // "sync this first" would be a claim about a platform nothing was learned
  // about, and the core read is independent of the count anyway.
  if (row.shortcutCount === 0) {
    return { kind: "say", text: "Sync this platform first — the core applies to the games it puts in Steam." };
  }
  if (core === undefined) return { kind: "say", text: "Reading the emulators for this platform…" };
  if (core === null) {
    return { kind: "say", text: "Could not read the emulators for this platform. Reopen the page to try again." };
  }
  if (!core.emulator_data_available) {
    return { kind: "say", text: "RetroDECK was not found, so there is no emulator list to choose from." };
  }
  // An EMPTY menu first, because it is the one case where the fallback fails
  // too. `_resolve_system` hands back the raw RomM slug for a platform its map
  // does not name, and `get_emulator_options` answers `available: true` with no
  // options for a system `es_systems.xml` does not list — `vic-20`,
  // `acorn-electron`, `nintendo-dsi`, `ps5`, `browser` and `win` are in neither
  // on this machine. RetroDECK's own launch then reads `command[1]` for the
  // system, finds nothing, logs "No valid emulator found for system" and exits
  // 1 (`libexec/run_game.sh`), so the games really do not start.
  if (core.emulators.length === 0) {
    return { kind: "say", text: "RetroDECK lists no emulator for this platform, so its games will not launch." };
  }
  // Then the not-bakeable menu, ahead of the counts because it is not one:
  // `active_core_label` is null when nothing ES-DE lists is BAKEABLE, which says
  // nothing about how many entries there are. A platform whose only entry is a
  // standalone emulator this RetroDECK has not installed lands here with one
  // option, and one with two uninstalled ones lands here with two — where a
  // count-shaped branch would have said "offers one emulator" or nothing at all.
  //
  // What the fallback then DOES splits the state in two, and the payload can
  // tell them apart. `run_game.sh` takes `command[1]` for the system when no
  // alternate emulator is set, and `options` keeps ES-DE's document order, so
  // `emulators[0]` IS that command. If its emulator is the one RetroDECK has
  // not installed, the fallback names a binary that is not there and the games
  // do not start — Apple I on this machine is exactly that: five commands, the
  // first `LinApple (Standalone)`, none installed and the two MAME forms
  // unbakeable for their quoting. Any other reason (`inject`, and the rest)
  // leaves an emulator RetroDECK can actually run.
  if (core.active_core_label === null) {
    const fallback = core.emulators[0];
    if (fallback?.reason === "not_installed") {
      return {
        kind: "say",
        text: `RetroDECK would launch these with ${fallback.label}, which is not installed — and nothing here can pin a different one.`,
      };
    }
    return {
      kind: "say",
      text: "None of this platform's emulators can be pinned from here, so RetroDECK picks one when a game launches.",
    };
  }
  // One count branch, and it is exactly true: an empty menu was answered two
  // branches up and a not-bakeable one the branch after it, so a single option
  // here is a single BAKEABLE one — there really is nothing to switch to.
  if (core.emulators.length === 1) {
    return { kind: "say", text: "This platform offers one emulator, so there is nothing to switch." };
  }
  return { kind: "pick", core };
}

/**
 * The line under the header when there is no core to pick, and the place a
 * refused switch is reported.
 *
 * There is no heading and no button: the button moved into the header line the
 * platform is already named on, which is where the game page keeps its own. The
 * save-compatibility warning moved with it — into the picker, where
 * `buildEmulatorMenu` has always rendered it, so a copy here was the same
 * sentence on the page that opens the menu carrying it.
 */
const CoreNotice: FC<{ row: PlatformRow; state: PlatformsPageState; offer: CoreOffer }> = ({ row, state, offer }) => (
  <>
    {offer.kind === "say" && <Muted>{offer.text}</Muted>}
    <GroupStatus state={state} slug={row.slug} scope="core" />
  </>
);

/**
 * What a download button shows instead of its label: a spinner while its own run
 * is going, a red `Failed` for the moment after one fails, or nothing.
 *
 * Both are keyed on the run's slug as well as the button's identity, because
 * `downloadPending` and `downloadFailed` name the button and every pane reads
 * the same state — without the slug a spinner would appear on the pane the
 * reader walked to. That pane shows disabled buttons and the line naming the
 * platform that is busy, which is what it always did.
 */
function downloadState(row: PlatformRow, state: PlatformsPageState, id: string): "spinner" | "failed" | null {
  if (state.busySlug !== row.slug) return null;
  if (state.downloadPending === id) return "spinner";
  return state.downloadFailed === id ? "failed" : null;
}

const FAILED_LABEL = <span style={{ color: RED }}>Failed</span>;

/** What the header's core clause reads where no option can be pinned: which of
 *  the three unpinnable states the platform is in, said in the fewest words the
 *  state supports. */
function fallbackLabel(noEmulator: boolean, fallbackMissing: boolean): string {
  if (noEmulator) return "no emulator";
  return fallbackMissing ? "no emulator installed" : "RetroDECK decides";
}

/** The label a download button carries right now — its own word unless its run
 *  is speaking. */
function downloadLabel(
  row: PlatformRow,
  state: PlatformsPageState,
  id: string,
  label: string,
  size: number,
): ReactNode {
  const showing = downloadState(row, state, id);
  if (showing === "spinner") return <Spinner width={size} height={size} />;
  return showing === "failed" ? FAILED_LABEL : label;
}

/**
 * A BIOS row's one action: fetch it, or remove the copy we fetched — or
 * nothing, which is a `null` the row needs in order to wrap itself in a
 * `Focusable`.
 *
 * The two buttons are alternatives in every state the pane can show, so one
 * narrow column carries both. They are not mutually exclusive by construction,
 * though: a file downloaded before an emu-atlas bump moved its placement is
 * absent at the new destination (so fetchable) and present at the recorded one
 * (so deletable). Download wins there, which is the useful half — the file the
 * core will look for is the one that is missing.
 *
 * **The delete's condition is `deletable_count` and nothing else.** That field
 * says how many of the plugin's own downloads a delete here would take, which
 * is the whole authority; `downloaded` is `os.path.exists` and is equally true
 * of `dolphin-emu/Sys/codehandler.bin`, which RetroDECK ships, no library can
 * hand back, and which sits one row above a real download on the GameCube pane.
 * The unlink itself re-reads the records and takes the path each one holds, so
 * this button cannot widen what it removes.
 *
 * A declared FOLDER offers it too, and that is a different rule from the one
 * that keeps a folder out of the downloads: there is no file to FETCH into a
 * name the emulator lists, but the files already inside it are ours wherever a
 * record names them. Its button carries the count and its delete is addressed
 * by the folder rather than by a file name, because no record carries the
 * folder's name.
 */
function rowAction(
  row: PlatformRow,
  state: PlatformsPageState,
  file: FirmwareRow,
  fetchable: Set<string>,
): ReactNode | null {
  const busy = state.busySlug !== null;
  if (fetchable.has(file.file_name) && !state.serverOffline) {
    return (
      <DialogButton
        style={ROW_BUTTON}
        disabled={busy}
        onClick={() => {
          state.downloadOne(row.slug, file.file_name);
        }}
      >
        {downloadLabel(row, state, file.file_name, "Download", 11)}
      </DialogButton>
    );
  }
  const count = file.deletable_count ?? 0;
  if (count === 0) return null;
  const folder = file.declared_kind === "directory";
  const confirm = () =>
    showModal(
      <ConfirmModal
        strTitle={folder ? `Delete ${count} file(s) in ${file.file_name}?` : `Delete ${file.file_name}?`}
        strDescription="This deletes only what this plugin downloaded, at the places it wrote them. Files your emulator came with, or that you put there yourself, are never touched — and a folder the emulator lists is never removed. Games that need them won't launch until you download them again."
        strOKButtonText="Delete"
        strCancelButtonText="Cancel"
        onOK={() => {
          if (folder) state.deleteBiosFolder(row.slug, file.local_path);
          else state.deleteBiosFile(row.slug, file.file_name);
        }}
      />,
    );
  return (
    <DialogButton style={ROW_BUTTON} disabled={busy} onClick={confirm}>
      <span style={{ color: RED }}>{folder ? `Delete (${count})` : "Delete"}</span>
    </DialogButton>
  );
}

const BiosSection: FC<{ row: PlatformRow; state: PlatformsPageState; firmware: FirmwarePlatformExt }> = ({
  row,
  state,
  firmware,
}) => {
  const files = firmware.files;
  // Display counts come from the backend aggregates (computed from the same
  // core-aware files); fall back to local derivation only if a payload omits
  // them. `total` is the LIBRARY's file count, not the row count — the rows
  // include files no library holds, and a progress ratio over those would
  // report work the user cannot do. The optional-missing breakdown stays a
  // local file-level axis — the level doesn't model it.
  const total = firmware.server_count ?? files.filter((f) => f.on_server).length;
  const done = firmware.local_count ?? files.filter((f) => f.on_server && f.downloaded).length;
  const allDone = done === total;
  const requiredFiles = files.filter((f) => f.required_by_active);
  const requiredCount = firmware.required_count ?? requiredFiles.length;
  const requiredDone = firmware.required_downloaded ?? requiredFiles.filter((f) => f.downloaded).length;
  const optionalMissing = files.filter((f) => f.wanted === "optional" && !f.required_by_active && !f.downloaded).length;
  // The ok/partial/missing DECISION is the backend's bios_level — "ready" means
  // all required files present. Fall back to the local count comparison only
  // when the level is absent from the payload.
  const requiredReady = firmware.bios_level == null ? requiredDone === requiredCount : firmware.bios_level === "ok";

  const isUnknown = firmware.bios_level === "unknown";
  const requiredWithheld = firmware.required_withheld ?? 0;
  const nothingEstablished = isUnknown && requiredWithheld === 0;
  const { summaryLabel, summaryDescription } = isUnknown
    ? getUnknownSummary(requiredWithheld)
    : getBiosSummary(requiredCount, requiredDone, requiredReady, optionalMissing, done, total);

  // The download affordances key off what is missing AND fetchable, never off
  // readiness: a required file the RomM library does not hold leaves the
  // platform not ready and still gives the user nothing to press here.
  //
  // `nothingEstablished` withdraws them entirely, and that is a PLATFORM
  // condition, never a per-file one: a platform whose reading finished may hold
  // plenty of files no installed emulator asks for — a PlayStation page
  // typically does — and every one of them stays fetchable, because "nothing
  // wants this" is an answer. Where nothing could be established there is no
  // answer to download against, so the pane says so instead of offering to
  // fetch files it cannot reason about. A declined READINESS verdict is not
  // that state and keeps its buttons: its rows were answered, and downloading
  // the files the library holds is the one thing that can still move the
  // platform along.
  //
  // A folder declaration is out whatever its state: the emulator lists that
  // name, so there is no file to fetch into it — what would satisfy it is a
  // BIOS image inside the folder, which is a different row.
  const fetchableMissing = nothingEstablished
    ? []
    : files.filter((f) => f.on_server && !f.downloaded && f.declared_kind !== "directory");
  const requiredMissing = fetchableMissing.filter((f) => f.required_by_active).length;
  const hasOptionalMissing = fetchableMissing.some((f) => !f.required_by_active);
  const showRequired = requiredMissing > 0 && !state.serverOffline;
  const showAll = !allDone && (hasOptionalMissing || requiredMissing > 0) && !state.serverOffline;
  const fetchable = new Set(fetchableMissing.map((f) => f.file_name));
  // What Delete BIOS would remove — a record count, not a library one. There is
  // no local fallback: the rows say nothing about who downloaded a file, so a
  // payload without the field offers no delete rather than guessing.
  const deletable = firmware.deletable_count ?? 0;
  const unanswered = files.filter((f) => f.wanted === "unknown").length;

  const confirmDeleteBios = () =>
    showModal(
      <ConfirmModal
        strTitle={`Delete BIOS files for ${row.name}?`}
        strDescription="This deletes only the BIOS files this plugin downloaded for this system. Files your emulator came with, or that you put there yourself, are left where they are. Games that need the deleted files won't launch until you download them again."
        strOKButtonText="Delete BIOS Files"
        strCancelButtonText="Cancel"
        onOK={() => state.deleteBios(row.slug)}
      />,
    );

  return (
    <>
      {/* The ratio is stated once, here, and takes the same mapping the list's
          dot takes — the header carried a second copy of it until the device
          pass, and its width was what wrapped that line three times. Two places
          state a platform's BIOS state and they now agree by construction. */}
      <SectionTitle title="BIOS files" note={summaryLabel} noteColor={biosColorForLevel(firmware.bios_level ?? null)} />
      <Muted>{summaryDescription}</Muted>
      {nothingEstablished && (
        <Muted>
          BIOS management is not supported for this system yet, so there is nothing to download here. You can still put
          BIOS files in your BIOS folder by hand.
        </Muted>
      )}
      {files.length > 0 && <BiosTableHeader />}
      {files.map((file) => (
        <BiosFileRow key={file.file_name} file={file} action={rowAction(row, state, file, fetchable)} />
      ))}
      {files.length > 0 && <BiosLegend files={files} />}
      {unanswered > 0 && (
        <Muted>
          {unanswered === 1 ? "1 file" : `${unanswered} files`} nothing installed could answer for. Report at
          github.com/danielcopper/romm-tender/issues if needed.
        </Muted>
      )}
      {/* One row of buttons rather than three stacked full-width ones. Each
          `ButtonItem` is a `Field` row around a button and costs the pane a row
          of its own; the three here fit on one.

          All three are always rendered and disable when there is nothing to do,
          the ruling the user gave for the Remove group and for the same reason:
          a button that vanishes is a state the reader has to work out, and on
          PS2 all three vanished at once. A disabled `DialogButton` is still a
          focus stop, so the row stays walkable.

          Delete is local-only (no server needed). Its number is the backend's
          `deletable_count` — the plugin's own download records that are still on
          disk, which is exactly what the delete unlinks. The library ratio
          counts a different set and was wrong here in both directions,
          including hiding the button over downloads RomM had stopped listing. */}
      <Focusable flow-children="horizontal" style={{ display: "flex", gap: "8px", padding: "2px 16px 6px" }}>
        <DialogButton
          style={FLAT_BUTTON}
          disabled={!showRequired || state.busySlug !== null}
          onClick={() => state.downloadRequired(row.slug)}
        >
          {downloadLabel(row, state, "required", `Download required (${requiredMissing})`, 12)}
        </DialogButton>
        <DialogButton
          style={FLAT_BUTTON}
          disabled={!showAll || state.busySlug !== null}
          onClick={() => state.downloadAll(row.slug)}
        >
          {downloadLabel(row, state, "all", "Download all", 12)}
        </DialogButton>
        <DialogButton
          style={FLAT_BUTTON}
          disabled={deletable === 0 || state.busySlug !== null}
          onClick={confirmDeleteBios}
        >
          <span style={{ color: RED }}>{`Delete BIOS (${deletable})`}</span>
        </DialogButton>
      </Focusable>
      <GroupStatus state={state} slug={row.slug} scope="bios" />
    </>
  );
};

/**
 * Taking a platform back out of Steam, and taking its save files off the disk.
 *
 * Neither button is hidden when its own count is zero. Hiding the group on the
 * shortcut count alone strands a platform whose shortcuts were removed but whose
 * saves remain: those saves are then unreachable, and this is the only page that
 * offers them.
 *
 * The saves count is its own read (`count_platform_saves`) rather than a number
 * taken from somewhere cheaper, because nowhere else has it — the delete finds
 * its files through the platform's installed ROMs and counts only what it
 * removed, afterwards.
 */
const RemoveSection: FC<{ row: PlatformRow; state: PlatformsPageState }> = ({ row, state }) => {
  const syncRunning = useSyncRunning();
  const saveCount = state.saveCountFor(row.slug);
  const confirmDeleteSaves = () =>
    showModal(
      <ConfirmModal
        strTitle={`Delete all save files for ${row.name}?`}
        strDescription="This will delete every local save file for ROMs on this platform. Any local changes that haven't been uploaded to RomM yet will be lost permanently. Make sure saves are synced first."
        strOKButtonText="Delete Save Files"
        strCancelButtonText="Cancel"
        onOK={() => state.deleteSaves(row)}
      />,
    );
  const confirmRemoveShortcuts = () =>
    showModal(
      <ConfirmModal
        strTitle={
          row.shortcutCount === null
            ? `Remove ${row.name} shortcuts?`
            : `Remove ${row.shortcutCount} ${row.name} shortcut${row.shortcutCount === 1 ? "" : "s"}?`
        }
        strDescription="This takes this platform's games out of your Steam library. Downloaded ROM files and save files are left where they are, and the games come back on the next sync while the platform stays enabled."
        strOKButtonText="Remove Shortcuts"
        strCancelButtonText="Cancel"
        onOK={() => state.removeShortcuts(row)}
      />,
    );
  return (
    <>
      {/* One row, and no REMOVE heading over it: both buttons say what they
          remove and are drawn in red, so a title above them names nothing the
          buttons do not — and it would cost the pane a row. */}
      <Focusable flow-children="horizontal" style={{ display: "flex", gap: "8px", padding: "6px 16px 4px" }}>
        <DialogButton
          style={FLAT_BUTTON}
          disabled={state.busySlug !== null || syncRunning || row.shortcutCount === 0}
          onClick={confirmRemoveShortcuts}
        >
          <span style={{ color: RED }}>
            {row.shortcutCount === null
              ? "Remove shortcuts"
              : `Remove ${row.shortcutCount} shortcut${row.shortcutCount === 1 ? "" : "s"}`}
          </span>
        </DialogButton>
        {/* Unread is not zero and is not a failure either, and the button must
            not look like either: while the count is still coming it is disabled
            and spins, which claims nothing. A pressable plain label would invite
            a press over an unknown set; a `0` would state an emptiness nobody
            established. A failed read is the third case and says so below. */}
        <DialogButton
          style={FLAT_BUTTON}
          disabled={state.busySlug !== null || saveCount === undefined || saveCount === 0}
          onClick={confirmDeleteSaves}
        >
          <span style={{ color: RED, display: "inline-flex", alignItems: "center", gap: "6px" }}>
            {saveCount === undefined && <Spinner width={12} height={12} />}
            {typeof saveCount === "number"
              ? `Delete ${saveCount} save file${saveCount === 1 ? "" : "s"}`
              : "Delete save files"}
          </span>
        </DialogButton>
      </Focusable>
      {/* The one read of the five whose failure had nothing to say. With the
          spinner above it that became worse rather than better — a spinner that
          never stops — so the two land together. */}
      {saveCount === null && (
        <Muted>Could not read how many save files this platform holds. Pick the platform again to retry.</Muted>
      )}
      {/* The hint was a ButtonItem `description`, attached to the one button it
          was about; under a row it has nowhere to hang, so it names that button
          instead. Only the shortcut removal is sync-gated — `main.py`'s
          `remove_platform_shortcuts` carries `@sync_active_blocked` and
          `delete_platform_saves` deliberately does not — so an unscoped sentence
          claims a restriction the backend does not impose. Scoping the sentence
          rather than gating the delete: the gate is the authority on what a sync
          blocks, and widening it to make a line true would be the tail wagging
          the dog. */}
      {syncRunning && <Muted>{`Removing shortcuts: ${SYNC_RUNNING_HINT}`}</Muted>}
      <GroupStatus state={state} slug={row.slug} scope="remove" />
    </>
  );
};

export const PlatformDetail: FC<{ row: PlatformRow; state: PlatformsPageState }> = ({ row, state }) => {
  const core = state.coreFor(row.slug);
  const firmware = row.firmware;
  const offer = coreOffer(row, core);
  // The core clause is absent while this platform's core read is in flight, and
  // stays absent if it failed — the read is issued per selection, so walking the
  // list shows each newly focused platform's header without a core until its own
  // answer lands. That, and a failed shortcut-count read dropping "· N in
  // Steam", are the two ways this line loses a piece; both failures now say so
  // on the pane, and the in-flight one is a beat rather than a state.
  //
  // The clause NAMES the core, and "Default" is not one of the names it can
  // take: `resolve_platform_label` answers with the real label in both ordinary
  // cases — the per-platform override where it still resolves, else the
  // es_systems default. Printing "Default" said the opposite of what was true.
  //
  // `null` splits in two, and only one half is a failure. With options on the
  // menu it means none is BAKEABLE, and `select_default_option` says what
  // follows: the plain RetroDECK launch is baked and RetroDECK resolves the
  // emulator itself, so the games still start and the clause says who is
  // choosing. With NO options there is nothing for RetroDECK to resolve either
  // — its own launch exits 1 — and the clause says so, in red.
  const activeLabel = core ? core.active_core_label : null;
  // Everything the clause says rests on the emulator list having been READ.
  // `get_emulator_options` answers `available: false` with an EMPTY list when
  // `es_systems.xml` cannot be read at all, so a clause keyed on the list's
  // length alone said "no emulator" in red over a state where nothing was
  // established — the definite failure claim this pane keeps having to remove,
  // and beside a sentence saying RetroDECK was not found. One premise, named
  // once, so the two readings below cannot drift apart.
  const emulatorsKnown = core != null && core.emulator_data_available;
  const noEmulator = emulatorsKnown && core.emulators.length === 0;
  // The fallback RetroDECK would use is missing, so the clause says that rather
  // than naming a core nothing can run. Same shape as the empty menu: a state
  // the games do not start in, in red.
  const fallbackMissing = emulatorsKnown && activeLabel === null && core.emulators[0]?.reason === "not_installed";
  // The platform-level twin of the game page's `activeCoreIsDefault`, read off
  // the payload's own `is_default`, which marks the single option
  // `select_default_option` picks. One expression feeds the clause AND the
  // icon, so the two cannot disagree — and no active label is muted rather than
  // gold, because `find` on a null label returns nothing and "not the default"
  // is not the same statement as "an override".
  const activeIsDefault = core?.emulators.find((option) => option.label === activeLabel)?.is_default ?? false;
  const coreColor = noEmulator || fallbackMissing ? RED : activeLabel === null || activeIsDefault ? MUTED : AMBER;
  // No clause at all where the list could not be read — the same silence a
  // failed or in-flight core read gets, and for the same reason.
  const coreClause = !emulatorsKnown
    ? null
    : { text: activeLabel ?? fallbackLabel(noEmulator, fallbackMissing), color: coreColor };

  return (
    <>
      {/* One header line rather than a Sync section: the toggle is in the list
          row, so what is left here is what the platform IS — and, since the
          device round, the core picker too: a full-width button under this line
          cost the pane a `Field`-height row and a warning line to say what the
          picker itself says. */}
      <Focusable
        flow-children="horizontal"
        style={{ display: "flex", alignItems: "baseline", gap: "10px", padding: "8px 16px 0" }}
      >
        <span style={{ fontSize: "16px", fontWeight: 600, color: "#dcdedf", minWidth: 0 }}>{row.name}</span>
        <span style={{ flex: "1 1 auto", fontSize: SECONDARY_FONT, color: MUTED }}>
          {`${row.romCount} on RomM`}
          {row.shortcutCount === null ? "" : ` · ${row.shortcutCount} in Steam`}
          {coreClause && <span style={{ color: coreClause.color }}>{` · ${coreClause.text}`}</span>}
        </span>
        {offer.kind === "pick" && (
          <DialogButton
            style={CORE_BUTTON}
            title="Emulator Core"
            disabled={state.busySlug !== null}
            onClick={(e: MouseEvent) =>
              showContextMenu(
                buildEmulatorMenu({
                  emulators: offer.core.emulators,
                  emulatorDataAvailable: offer.core.emulator_data_available,
                  activeLabel: offer.core.active_core_label,
                  // Null on purpose: this pane IS the platform level, so marking
                  // an entry "(system)" would restate where the reader already is.
                  platformCoreLabel: null,
                  onPick: (label) => state.changeCore(row.slug, label),
                }),
                getEventTarget(e),
              )
            }
          >
            <FaMicrochip size={16} color={coreColor} />
          </DialogButton>
        )}
      </Focusable>
      {/* The count is what failed, not the removal: taking the platform's games
          out of Steam needs only the slug. So the line says the number is
          missing and stops there — the buttons below stay live. */}
      {state.shortcutCountsFailed && (
        <Muted>
          Could not read how many of these games are in Steam. Removing them still works, it just cannot say how many.
          Reopen the page to try again.
        </Muted>
      )}
      {state.removalProgress?.slug === row.slug && (
        <Muted>{`Removing ${state.removalProgress.removed} of ${state.removalProgress.total}…`}</Muted>
      )}
      <BusyElsewhere row={row} state={state} />
      <CoreNotice row={row} state={state} offer={offer} />
      {/* A failed read is said on EVERY pane, not only the ones with no entry.
          A failed refresh does not clear the map, so a platform that has an
          entry keeps showing pre-change rows — which is exactly where a reader
          needs telling, and where the notice used to be silent while appearing
          on the panes that had least to be wrong about. The two panes need
          different sentences because only one of them has stale rows to warn
          about. */}
      {state.firmwareFailed && (
        <Muted>
          {state.firmwareHeld
            ? "Could not re-read the BIOS state, so what is below may be out of date. Reopen the page to try again."
            : "Could not read the BIOS state. Reopen the page to try again."}
        </Muted>
      )}
      {firmware ? (
        <BiosSection row={row} state={state} firmware={firmware} />
      ) : (
        <>
          <SectionTitle title="BIOS files" />
          {/* A failed read and a platform the overview has nothing to say about
              arrive the same way — an absent entry — and they are different
              sentences: one is a question that could not be asked, the other a
              finished answer. A failed RE-read is the second again: the answer
              set still stands, this platform's part of it is still "nothing",
              and the notice above says the whole of it may be stale. */}
          {(!state.firmwareFailed || state.firmwareHeld) && (
            <Muted>Nothing is known about this platform&apos;s BIOS files.</Muted>
          )}
        </>
      )}
      <RemoveSection row={row} state={state} />
    </>
  );
};
