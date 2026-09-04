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
import type { FirmwarePlatformExt } from "../../types";
import { biosColorForLevel } from "../../utils/biosColor";
import { biosFileNote } from "../../utils/biosFileNote";
import { buildEmulatorMenu } from "../../utils/emulatorMenu";
import { getEventTarget } from "../../utils/events";
import { SYNC_RUNNING_HINT, useSyncRunning } from "../../utils/syncRunning";
import type { CoreAnswer, PlatformRow, PlatformsPageState, StatusScope } from "./usePlatformsPage";

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
 * What their relationship is inside Steam is deliberately NOT claimed here.
 * `@decky/ui` finds `ButtonItem` with a webpack prop-list regex over
 * `CommonUIModule` (`components/ButtonItem.js`), so the package can say nothing
 * about what it renders, and the module's own render function is not in this
 * install's `steamui` chunks to read — the one `childrenContainerWidth` hit
 * there is a call site.
 */
const FLAT_BUTTON = { flex: "1 1 auto", minWidth: 0, padding: "6px 10px", fontSize: "13px" } as const;

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
      summaryDescription: `${requiredCount - requiredDone} required missing — games may not launch`,
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
 * could not read, say. Zero is the older shape: no installed emulator's
 * answer could be established for the platform at all, which splits again on
 * whether there are rows to point at, because a platform whose emulators are all
 * standalone has none and "0 file(s) nothing installed could answer for" would
 * read as a finished count of nothing rather than as silence.
 */
function getUnknownSummary(requiredWithheld: number, total: number) {
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
    summaryDescription:
      total > 0
        ? `${total} file(s) nothing installed could answer for`
        : "Nothing installed could answer for this system",
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
 */
function libraryMark(file: FirmwareRow): typeof LIBRARY_MARK | null {
  return file.on_server === false ? LIBRARY_MARK : null;
}

const SectionTitle: FC<{ title: string; note?: string }> = ({ title, note }) => (
  <div style={{ display: "flex", alignItems: "baseline", gap: "8px", padding: "12px 16px 4px" }}>
    <span style={{ fontSize: "12px", fontWeight: 600, letterSpacing: "0.5px", color: "#dcdedf" }}>
      {title.toUpperCase()}
    </span>
    {note && <span style={{ fontSize: "11px", color: MUTED }}>{note}</span>}
  </div>
);

const Muted: FC<{ children: ReactNode }> = ({ children }) => (
  <div style={{ fontSize: "11px", color: MUTED, padding: "0 16px 6px" }}>{children}</div>
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

// File, On disk, Contents, and the row's own button. `On disk` holds marks and
// nothing else, so it is sized for its own heading rather than for a sentence,
// and what it gives up goes to the file name.
const TABLE_COLUMNS = "1fr 68px 92px 116px";

const BiosTableHeader: FC = () => (
  // Column names accompany the rows below them and scroll with them; making the
  // header a focus stop would add a step that leads nowhere.
  <div
    style={{
      display: "grid",
      gridTemplateColumns: TABLE_COLUMNS,
      gap: "8px",
      padding: "0 16px 4px",
      fontSize: "11px",
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
 * belongs to the row's On-disk note, whose wording is {@link biosFileNote}'s.
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
 * Full width, because the alternative is a 68px cell wrapping one sentence
 * across three lines — the cost the marks were introduced to stop paying. A
 * note here is also rare: measured over the `.info` corpus, the only rows that
 * carry one are the handful RetroDECK supplies itself and PS2's folder row.
 *
 * Each image string is the resolver's verbatim, and `pre-wrap` keeps the column
 * padding PCSX2 puts in its own option labels — that alignment is what makes a
 * line matchable against the emulator's own picker. They sit under the row
 * rather than in the Contents cell because the cell is 92px and one of these
 * labels is not; the cell counts them instead.
 */
const BiosRowLines: FC<{ lines: string[] }> = ({ lines }) =>
  lines.length === 0 ? null : (
    <div style={{ display: "flex", flexDirection: "column", gap: "2px", marginLeft: "18px", marginTop: "2px" }}>
      {lines.map((line) => (
        <div key={line} style={{ fontSize: "11px", color: MUTED, whiteSpace: "pre-wrap" }}>
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
 * (which is `os.path.basename` of the declared path) falls into four shapes:
 *
 * | 245 | 35% | it IS the name — `"macventure.dat"`                          |
 * | 328 | 47% | the name, a space, then prose — `"scph5500.bin (PS1 JP BIOS)"` |
 * | 115 | 17% | the same, but the name carries its directory — `"dc/dc_boot.bin (Dreamcast BIOS)"` |
 * |   7 |  1% | the first token is not the name — five that never mention it (`"Dolphin 'Sys' folder"`, two upstream typos), and two whose name contains a space |
 *
 * So the rule is: if the description's first token names this file — as itself
 * or at the end of a path — drop that token and keep the rest. That fires on
 * 688 of the 695 and on the no-placement case. Of the seven left, five say
 * something real and are printed whole; the other two do repeat the name and
 * are printed whole anyway, because a name with a space in it (`"7800 BIOS
 * (U).rom"`) is not one token and a rule that scanned for it anywhere in the
 * words would cut into prose that merely quotes it. The prose is kept verbatim,
 * parentheses and all, because it is the packager's own words and
 * re-punctuating it is a second way to be wrong.
 */
function fileDescription(file: FirmwareRow): string | null {
  const description = file.description.trim();
  if (!description) return null;
  const [head, ...tail] = description.split(" ");
  const names = head === undefined ? "" : (head.split("/").pop() ?? "");
  if (names !== file.file_name) return description;
  const rest = tail.join(" ").trim();
  return rest || null;
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
    { glyph: "✗", color: AMBER, text: "missing; nothing asked for it" },
    { glyph: "✓", color: AMBER, text: "here; nothing asked for it" },
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
        fontSize: "11px",
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

const BiosFileRow: FC<{ file: FirmwareRow; download: ReactNode }> = ({ file, download }) => {
  const { note, lines, fromLibrary } = biosFileNote(file);
  const mark = diskMark(file);
  const library = libraryMark(file);
  const description = fileDescription(file);
  // The library note is the one sentence the cell's second mark now carries, and
  // on a platform whose library holds little it was the same words under nearly
  // every row. Everything else moves under the row rather than into the cell.
  const rowLines = fromLibrary ? [] : [...(note ? [note] : []), ...lines];
  const cells = (
    <>
      <div style={{ display: "grid", gridTemplateColumns: TABLE_COLUMNS, gap: "8px", alignItems: "center" }}>
        <span style={{ minWidth: 0, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
          {file.file_name}
          {description && <span style={{ fontSize: "11px", color: MUTED }}>{` ${description}`}</span>}
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
        <span style={{ color: MUTED }}>{contentsCell(file)}</span>
        <span>{download}</span>
      </div>
      <BiosRowLines lines={rowLines} />
    </>
  );
  if (download) return <div style={{ padding: "4px 16px" }}>{cells}</div>;
  // A row with nothing to press still has to be reachable, or the reader cannot
  // scroll past it to the rows below: the activate handler is what makes a
  // Focusable a focus stop.
  return (
    <Focusable onActivate={() => {}} style={{ padding: "4px 16px" }}>
      {cells}
    </Focusable>
  );
};

/**
 * The core row: one button under the header, or one sentence saying why there is
 * nothing to press.
 *
 * **No section heading.** The header line above already names the active core,
 * so a title over a single button would restate it — and on the Deck's body a
 * heading is a row the pane cannot pay for.
 */
const CoreSection: FC<{ row: PlatformRow; state: PlatformsPageState; core: CoreAnswer }> = ({ row, state, core }) => {
  // Strictly zero, so an unread shortcut count does not withdraw the picker:
  // "sync this first" would be a claim about a platform nothing was learned
  // about, and the core read is independent of the count anyway.
  if (row.shortcutCount === 0) {
    return <Muted>Sync this platform first — the core applies to the games it puts in Steam.</Muted>;
  }
  if (core === undefined) return <Muted>Reading the emulators for this platform…</Muted>;
  if (core === null) {
    return (
      <>
        <Muted>Could not read the emulators for this platform. Reopen the page to try again.</Muted>
        <GroupStatus state={state} slug={row.slug} scope="core" />
      </>
    );
  }
  if (!core.emulator_data_available) {
    return <Muted>RetroDECK was not found, so there is no emulator list to choose from.</Muted>;
  }
  if (core.emulators.length < 2) {
    return <Muted>This platform offers one emulator, so there is nothing to switch.</Muted>;
  }
  return (
    <>
      <Focusable flow-children="horizontal" style={{ display: "flex", padding: "0 16px 4px" }}>
        <DialogButton
          style={FLAT_BUTTON}
          disabled={state.busySlug !== null}
          onClick={(e: MouseEvent) =>
            showContextMenu(
              buildEmulatorMenu({
                emulators: core.emulators,
                emulatorDataAvailable: core.emulator_data_available,
                activeLabel: core.active_core_label,
                // Null on purpose: this pane IS the platform level, so marking
                // an entry "(system)" would restate where the reader already is.
                platformCoreLabel: null,
                onPick: (label) => state.changeCore(row.slug, label),
              }),
              getEventTarget(e),
            )
          }
        >
          Change core ›
        </DialogButton>
      </Focusable>
      <Muted>Switching cores may affect save compatibility.</Muted>
      <GroupStatus state={state} slug={row.slug} scope="core" />
    </>
  );
};

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
    ? getUnknownSummary(requiredWithheld, total)
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
      <SectionTitle title="BIOS files" note={summaryLabel} />
      <Muted>{summaryDescription}</Muted>
      {nothingEstablished && (
        <Muted>
          BIOS management is not supported for this system yet, so there is nothing to download here. You can still put
          BIOS files in your BIOS folder by hand.
        </Muted>
      )}
      {files.length > 0 && <BiosTableHeader />}
      {files.map((file) => (
        <BiosFileRow
          key={file.file_name}
          file={file}
          download={
            fetchable.has(file.file_name) && !state.serverOffline ? (
              <DialogButton
                style={{ padding: "4px 0", minWidth: 0, fontSize: "12px" }}
                disabled={state.busySlug !== null}
                onClick={() => state.downloadOne(row.slug, file.file_name)}
              >
                Download
              </DialogButton>
            ) : null
          }
        />
      ))}
      {files.length > 0 && <BiosLegend files={files} />}
      {unanswered > 0 && (
        <Muted>
          {unanswered} file(s) nothing installed could answer for. Report at github.com/danielcopper/romm-tender/issues
          if needed.
        </Muted>
      )}
      {/* One row of buttons rather than three stacked full-width ones. Each
          `ButtonItem` is a `Field` row around a button and costs the pane a row
          of its own; the three here fit on one.

          Delete is local-only (no server needed) and shown only when there is at
          least one file it would actually remove. That number is the backend's
          `deletable_count` — the plugin's own download records that are still on
          disk, which is exactly what the delete unlinks. The library ratio
          counts a different set and was wrong here in both directions,
          including hiding the button over downloads RomM had stopped listing. */}
      {(showRequired || showAll || deletable > 0) && (
        <Focusable flow-children="horizontal" style={{ display: "flex", gap: "8px", padding: "2px 16px 6px" }}>
          {showRequired && (
            <DialogButton
              style={FLAT_BUTTON}
              disabled={state.busySlug !== null}
              onClick={() => state.downloadRequired(row.slug)}
            >
              {`Download required (${requiredMissing})`}
            </DialogButton>
          )}
          {showAll && (
            <DialogButton
              style={FLAT_BUTTON}
              disabled={state.busySlug !== null}
              onClick={() => state.downloadAll(row.slug)}
            >
              Download all
            </DialogButton>
          )}
          {deletable > 0 && (
            <DialogButton style={FLAT_BUTTON} disabled={state.busySlug !== null} onClick={confirmDeleteBios}>
              <span style={{ color: RED }}>{`Delete BIOS (${deletable})`}</span>
            </DialogButton>
          )}
        </Focusable>
      )}
      <GroupStatus state={state} slug={row.slug} scope="bios" />
    </>
  );
};

/**
 * Taking a platform back out of Steam, and taking its save files off the disk.
 *
 * Both buttons are always rendered and disable when there is nothing to delete;
 * neither is ever hidden. Hiding the group on the shortcut count alone strands a
 * platform whose shortcuts were removed but whose saves remain — those saves are
 * then unreachable from this page, and this is the only page that offers them.
 *
 * The saves count is its own read (`count_platform_saves`), because nothing else
 * knows it: the delete finds its files through the platform's installed ROMs and
 * counts only what it removed, afterwards.
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
      {saveCount === null && <Muted>Could not read how many save files this platform holds.</Muted>}
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
  // The core clause is absent while this platform's core read is in flight, and
  // stays absent if it failed — the read is issued per selection, so walking the
  // list shows each newly focused platform's header without a core until its own
  // answer lands. That, and a failed shortcut-count read dropping "· N in
  // Steam", are the two ways this line loses a piece; both failures now say so
  // on the pane, and the in-flight one is a beat rather than a state.
  const coreLabel = core ? (core.active_core_label ?? "Default") : null;
  const requiredCount = firmware?.required_count ?? 0;
  const biosBadge =
    firmware && requiredCount > 0 ? `BIOS ${firmware.required_downloaded ?? 0} / ${requiredCount}` : null;

  return (
    <>
      {/* One header line rather than a Sync section: the toggle is in the list
          row, so what is left here is what the platform IS. */}
      <div style={{ display: "flex", alignItems: "baseline", gap: "10px", padding: "8px 16px 0" }}>
        <span style={{ fontSize: "16px", fontWeight: 600, color: "#dcdedf", minWidth: 0 }}>{row.name}</span>
        <span style={{ flex: "1 1 auto", fontSize: "11px", color: MUTED }}>
          {`${row.romCount} on RomM`}
          {row.shortcutCount === null ? "" : ` · ${row.shortcutCount} in Steam`}
          {coreLabel ? ` · ${coreLabel}` : ""}
        </span>
        {biosBadge && (
          <span style={{ fontSize: "12px", color: biosColorForLevel(firmware?.bios_level ?? null) }}>{biosBadge}</span>
        )}
      </div>
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
      <CoreSection row={row} state={state} core={core} />
      {/* A failed read is said on EVERY pane, not only the ones with no entry.
          A failed refresh does not clear the map, so a platform that has an
          entry keeps showing pre-change rows — which is exactly where a reader
          needs telling, and where the notice used to be silent while appearing
          on the panes that had least to be wrong about. The two panes need
          different sentences because only one of them has stale rows to warn
          about. */}
      {state.firmwareFailed && (
        <Muted>
          {firmware
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
              finished answer, which is why only the second is said here. */}
          {!state.firmwareFailed && <Muted>Nothing is known about this platform&apos;s BIOS files.</Muted>}
        </>
      )}
      <RemoveSection row={row} state={state} />
    </>
  );
};
