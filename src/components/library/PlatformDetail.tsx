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
import { ButtonItem, ConfirmModal, DialogButton, Focusable, showContextMenu, showModal } from "@decky/ui";
import type { FirmwarePlatformExt, FirmwareWanted } from "../../types";
import { biosColorForLevel } from "../../utils/biosColor";
import { biosFileNote } from "../../utils/biosFileNote";
import { buildEmulatorMenu } from "../../utils/emulatorMenu";
import { getEventTarget } from "../../utils/events";
import { SYNC_RUNNING_HINT, useSyncRunning } from "../../utils/syncRunning";
import type { CoreAnswer, PlatformRow, PlatformsPageState, StatusScope } from "./usePlatformsPage";

/**
 * How each of the four `wanted` values reads on a file row. `not_needed` is
 * spelled out rather than shortened: "not needed" is a statement about every
 * installed emulator, and the row beside it saying "unknown" is the absence of
 * one, so the two must not look like near-synonyms.
 */
const WANTED_LABELS: Record<FirmwareWanted, string> = {
  needed: "needed",
  optional: "optional",
  not_needed: "not needed",
  unknown: "unknown",
};

const MUTED = "#8f98a0";

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
 * The colour a file row's on-disk state is drawn in. NOT
 * {@link biosColorForLevel}: that maps the platform's four-valued readiness
 * verdict, and this is a per-file state with its own rules — it reads the row's
 * VERDICT rather than `downloaded`, because for a declared folder the two come
 * apart: the folder is there on every RetroDECK install and what satisfies the
 * core is a file inside it. A payload with no verdict falls back to
 * `downloaded`, which is what the verdict is for a plain file.
 */
function fileColor(file: FirmwareRow): string {
  const verdict = file.satisfied === undefined ? file.downloaded : file.satisfied;
  // A row nothing could judge is amber for the same reason a row nothing could
  // be asked about is.
  if (file.wanted === "unknown" || verdict === null) return "#d4a72c";
  if (verdict) return "#5ba32b";
  if (file.required_by_active) return "#d94126";
  return MUTED;
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
 * An action disables every platform's actions, not just its own: the prune
 * lease and the firmware re-read are page-wide, so two at once would contend.
 * The line that would explain the wait — {@link GroupStatus} — is bound to the
 * platform the action belongs to, so walking away from a running removal leaves
 * a pane full of disabled buttons and nothing said. This is what it says.
 */
const BusyElsewhere: FC<{ row: PlatformRow; state: PlatformsPageState }> = ({ row, state }) => {
  if (state.busySlug === null || state.busySlug === row.slug) return null;
  const other = state.rows.get(state.busySlug)?.name ?? "another platform";
  return <Muted>{`Working on ${other} — actions here are paused until it finishes.`}</Muted>;
};

const TABLE_COLUMNS = "1fr 104px 92px 116px";

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
    <span>Wanted</span>
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
 * The images a folder holds, one per line under the row they belong to.
 *
 * Each string is the resolver's verbatim, and `pre-wrap` keeps the column
 * padding PCSX2 puts in its own option labels — that alignment is what makes a
 * line matchable against the emulator's own picker. They sit under the row
 * rather than in the Contents cell because the cell is 92px and one of these
 * labels is not; the cell counts them instead.
 */
const BiosFileLines: FC<{ lines: string[] }> = ({ lines }) =>
  lines.length === 0 ? null : (
    <div style={{ display: "flex", flexDirection: "column", gap: "2px", marginLeft: "18px", marginTop: "2px" }}>
      {lines.map((line) => (
        <div key={line} style={{ fontSize: "11px", color: MUTED, whiteSpace: "pre-wrap" }}>
          {line}
        </div>
      ))}
    </div>
  );

const BiosFileRow: FC<{ file: FirmwareRow; download: ReactNode }> = ({ file, download }) => {
  const { note, lines } = biosFileNote(file);
  const cells = (
    <>
      <div style={{ display: "grid", gridTemplateColumns: TABLE_COLUMNS, gap: "8px", alignItems: "center" }}>
        <span style={{ minWidth: 0 }}>
          <span style={{ display: "block", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
            {file.file_name}
          </span>
          <span style={{ fontSize: "11px", color: MUTED }}>
            {file.description ? `${file.description} · ` : ""}
            {WANTED_LABELS[file.wanted]}
          </span>
        </span>
        <span style={{ color: fileColor(file) }}>
          {file.downloaded ? "Yes" : "Missing"}
          {note && <span style={{ display: "block", fontSize: "11px", color: MUTED }}>{note}</span>}
        </span>
        <span style={{ color: MUTED }}>{contentsCell(file)}</span>
        <span>{download}</span>
      </div>
      <BiosFileLines lines={lines} />
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

const CoreSection: FC<{ row: PlatformRow; state: PlatformsPageState; core: CoreAnswer }> = ({ row, state, core }) => {
  if (row.shortcutCount === 0) {
    return (
      <>
        <SectionTitle title="Emulator core" />
        <Muted>Sync this platform first — the core applies to the games it puts in Steam.</Muted>
      </>
    );
  }
  if (core === undefined) {
    return (
      <>
        <SectionTitle title="Emulator core" />
        <Muted>Reading the emulators for this platform…</Muted>
      </>
    );
  }
  if (core === null) {
    return (
      <>
        <SectionTitle title="Emulator core" />
        <Muted>Could not read the emulators for this platform. Reopen the page to try again.</Muted>
        <GroupStatus state={state} slug={row.slug} scope="core" />
      </>
    );
  }
  if (!core.emulator_data_available) {
    return (
      <>
        <SectionTitle title="Emulator core" />
        <Muted>RetroDECK was not found, so there is no emulator list to choose from.</Muted>
      </>
    );
  }
  if (core.emulators.length < 2) {
    return (
      <>
        <SectionTitle title="Emulator core" />
        <Muted>This platform offers one emulator, so there is nothing to switch.</Muted>
      </>
    );
  }
  return (
    <>
      <SectionTitle title="Emulator core" />
      <ButtonItem
        layout="below"
        bottomSeparator="none"
        disabled={state.busySlug !== null}
        onClick={(e: Event) =>
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
        Change core
      </ButtonItem>
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
  const hasRequiredMissing = fetchableMissing.some((f) => f.required_by_active);
  const hasOptionalMissing = fetchableMissing.some((f) => !f.required_by_active);
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
      {unanswered > 0 && (
        <Muted>
          {unanswered} file(s) nothing installed could answer for. Report at github.com/danielcopper/romm-tender/issues
          if needed.
        </Muted>
      )}
      {hasRequiredMissing && !state.serverOffline && (
        <ButtonItem
          layout="below"
          bottomSeparator="none"
          disabled={state.busySlug !== null}
          onClick={() => state.downloadRequired(row.slug)}
        >
          Download required
        </ButtonItem>
      )}
      {!allDone && (hasOptionalMissing || hasRequiredMissing) && !state.serverOffline && (
        <ButtonItem
          layout="below"
          bottomSeparator="none"
          disabled={state.busySlug !== null}
          onClick={() => state.downloadAll(row.slug)}
        >
          Download all
        </ButtonItem>
      )}
      {/* Delete is local-only (no server needed) and shown only when there is at
          least one file it would actually remove. That number is the backend's
          `deletable_count` — the plugin's own download records that are still on
          disk, which is exactly what the delete unlinks. The library ratio
          counts a different set and was wrong here in both directions,
          including hiding the button over downloads RomM had stopped listing. */}
      {deletable > 0 && (
        <ButtonItem
          layout="below"
          bottomSeparator="none"
          disabled={state.busySlug !== null}
          onClick={confirmDeleteBios}
        >
          <span style={{ color: "#d94126" }}>{`Delete BIOS (${deletable})`}</span>
        </ButtonItem>
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
        strTitle={`Remove ${row.shortcutCount} ${row.name} shortcut${row.shortcutCount === 1 ? "" : "s"}?`}
        strDescription="This takes this platform's games out of your Steam library. Downloaded ROM files and save files are left where they are, and the games come back on the next sync while the platform stays enabled."
        strOKButtonText="Remove Shortcuts"
        strCancelButtonText="Cancel"
        onOK={() => state.removeShortcuts(row)}
      />,
    );
  return (
    <>
      <SectionTitle title="Remove" />
      <ButtonItem
        layout="below"
        bottomSeparator="none"
        disabled={state.busySlug !== null || syncRunning || row.shortcutCount === 0}
        description={syncRunning ? SYNC_RUNNING_HINT : undefined}
        onClick={confirmRemoveShortcuts}
      >
        <span style={{ color: "#d94126" }}>
          {`Remove ${row.shortcutCount} shortcut${row.shortcutCount === 1 ? "" : "s"}`}
        </span>
      </ButtonItem>
      <ButtonItem
        layout="below"
        bottomSeparator="none"
        disabled={state.busySlug !== null || saveCount === 0}
        onClick={confirmDeleteSaves}
      >
        <span style={{ color: "#d94126" }}>
          {typeof saveCount === "number"
            ? `Delete ${saveCount} save file${saveCount === 1 ? "" : "s"}`
            : "Delete save files"}
        </span>
      </ButtonItem>
      <GroupStatus state={state} slug={row.slug} scope="remove" />
    </>
  );
};

export const PlatformDetail: FC<{ row: PlatformRow; state: PlatformsPageState }> = ({ row, state }) => {
  const core = state.coreFor(row.slug);
  const firmware = row.firmware;
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
          {`${row.romCount} on RomM · ${row.shortcutCount} in Steam`}
          {coreLabel ? ` · ${coreLabel}` : ""}
        </span>
        {biosBadge && (
          <span style={{ fontSize: "12px", color: biosColorForLevel(firmware?.bios_level ?? null) }}>{biosBadge}</span>
        )}
      </div>
      {state.removalProgress?.slug === row.slug && (
        <Muted>{`Removing ${state.removalProgress.removed} of ${state.removalProgress.total}…`}</Muted>
      )}
      <BusyElsewhere row={row} state={state} />
      <CoreSection row={row} state={state} core={core} />
      {firmware ? (
        <BiosSection row={row} state={state} firmware={firmware} />
      ) : (
        <>
          <SectionTitle title="BIOS files" />
          <Muted>Nothing is known about this platform&apos;s BIOS files.</Muted>
        </>
      )}
      <RemoveSection row={row} state={state} />
    </>
  );
};
