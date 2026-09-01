/**
 * BiosTab — the BIOS & Emulator pane of the RomM game detail panel.
 *
 * Render only. Everything it shows arrives as props, because the requirement
 * (`biosStatus`) and the core it is shown against (`coreInfo`) have to reach a
 * render TOGETHER. On a core change the panel reads both in one `Promise.all`
 * and folds them into one update; split across two owners the change would land
 * as two renders, and in between the pane highlights the previous core's line
 * and names it in the "Active Core" row against a requirement set that has
 * already moved. The per-file LABELS are not part of that window —
 * `coreInfo.emulators` is the platform's core list, which a core switch does not
 * change.
 *
 * Two axes are rendered side by side and must not be conflated. A file's
 * `wanted` is what the whole machine says about it; `required_by_active` is what
 * the core this game launches with says. The readiness line is about the second
 * — a file another core demands is not a missing prerequisite for this launch —
 * while the rows below it show the first, so nothing is silently dropped from
 * the list for belonging to a core the user is not using.
 *
 * A row can also be a file an emulator wants that the RomM library does not hold
 * (`on_server` false). It is missing in the fullest sense — and unfixable from
 * anywhere in the plugin — so it says so rather than looking like a download
 * nobody has started.
 *
 * CSS classes prefixed with `romm-panel-` are injected separately by
 * styleInjector.
 */

import { FC, type ReactElement } from "react";
import type { BiosFileStatus, BiosLevel, BiosStatus, CoreInfo, FirmwareWanted } from "../types";
import { biosColorForLevel } from "../utils/biosColor";
import { infoRow, section } from "./panelSection";

interface BiosTabProps {
  /** The platform's BIOS requirement, or null when its core needs none — in
   *  which case there is no tab to render. */
  biosStatus: BiosStatus | null;
  /** Backend-computed readiness verdict driving the status-dot color;
   *  null whenever there is no requirement. */
  biosLevel: BiosLevel | null;
  /** Active core + available cores, from the dedicated `get_platform_core_info`
   *  path (#923) — never derived from `biosStatus`. */
  coreInfo: CoreInfo | null;
  isActive: boolean;
}

/** Render the per-core lines under a BIOS file — one row per core that uses it. */
function buildBiosCoreLines(
  cores: Record<string, { required: boolean }>,
  coreLabelMap: Record<string, string>,
  activeCore: string | null | undefined,
): ReactElement[] {
  return Object.entries(cores).map(([coreSo, coreData]) => {
    const label = coreLabelMap[coreSo] || coreSo.replace(/_libretro$/, "");
    const suffix = coreData.required ? " (required)" : " (optional)";
    // Highlight the resolved active core's line (#955). active_core is the
    // core's `.so`, same identifier space as the cores keys; a null/undefined
    // active core matches nothing.
    const isActiveCore = coreSo === activeCore;
    return (
      <div
        key={`core-${coreSo}`}
        style={{
          color: isActiveCore ? "#d4a72c" : "rgba(255, 255, 255, 0.5)",
          fontSize: "12px",
          fontWeight: isActiveCore ? "bold" : "normal",
        }}
      >
        {`${label}${suffix}`}
      </div>
    );
  });
}

/**
 * The header line: status dot plus the readiness phrasing this surface uses.
 *
 * The sentence and the ratio beside it are the SAME axis (#1762). Where there
 * are required files the ratio counts those; where there are none it counts
 * every file, because that is then the only axis there is. Printing a
 * required-file sentence next to an all-files ratio described two different sets
 * as one line.
 */
function buildBiosHeader(bios: BiosStatus, biosLevel: BiosTabProps["biosLevel"]): ReactElement[] {
  const localCount = bios.local_count ?? 0;
  const serverCount = bios.server_count ?? 0;
  const reqCount = bios.required_count ?? 0;
  const reqDone = bios.required_downloaded ?? 0;

  // Color is sourced from the backend unknown/ok/partial/missing verdict via the
  // shared helper — never re-derived here. The verbose phrasing below stays this
  // surface's own concern (per-surface wording).
  const biosColor = biosColorForLevel(biosLevel);
  let biosLabel: string;
  if (biosLevel === "unknown") {
    // Nothing installed could say whether these files are wanted. Honest text
    // over the neutral grey dot, never a false "All ready".
    biosLabel = "BIOS requirement unknown";
  } else if (reqCount > 0) {
    biosLabel =
      reqDone >= reqCount
        ? `All required ready (${reqDone}/${reqCount})`
        : `${reqDone}/${reqCount} required files ready`;
  } else {
    biosLabel = bios.all_downloaded
      ? `All ready (${localCount}/${serverCount})`
      : `${localCount}/${serverCount} files ready`;
  }

  return [
    <div key="bios-title" className="romm-panel-section-title" style={{ marginBottom: "8px" }}>
      BIOS
    </div>,
    <div key="bios-row" className="romm-panel-status-inline">
      <span className="romm-status-dot" style={{ backgroundColor: biosColor }} />
      <span className="romm-panel-value">{biosLabel}</span>
    </div>,
  ];
}

/** The dot beside one file row: what it means for THIS launch, then for others. */
function fileDotColor(file: BiosFileStatus): string {
  if (file.downloaded) return "#5ba32b";
  if (file.required_by_active) return "#d94126";
  // Missing and not required here, but demanded by some other installed core:
  // amber, because switching cores would make it a blocker.
  const requiredElsewhere = Object.values(file.cores ?? {}).some((c) => c.required);
  return requiredElsewhere ? "#d4a72c" : "#8f98a0";
}

/**
 * The note after a file's name. Empty for every row this pane has always shown —
 * downloaded state is the dot's job, and repeating it in text would be a
 * redesign rather than a fix. The one row that needs words is the file no page
 * in the plugin can fetch, because nothing else on screen tells it apart from a
 * download nobody has started yet.
 */
function fileNote(file: BiosFileStatus): string {
  return file.on_server === false ? " — missing, not in your RomM library" : "";
}

/**
 * One row per firmware file an installed emulator asks for, plus a note for the rest.
 *
 * The rows are the files with an owning emulator — the ones whose per-core lines
 * say something. The note below them keeps the two remaining answers apart
 * (#1762): files nothing asks for are a finished answer, files nothing could be
 * asked about are not, and the old single line called both "not required by any
 * known core" while counting neither.
 */
function buildBiosFileList(bios: BiosStatus, coreInfo: CoreInfo | null): ReactElement[] {
  // Build core_so -> label lookup from the dedicated core-info path (#923).
  // Only libretro emulators carry a core_so (a standalone emulator has none),
  // so filter those in for the per-core BIOS lines.
  const coreLabelMap: Record<string, string> = {};
  for (const e of coreInfo?.emulators ?? []) {
    if (e.core_so) coreLabelMap[e.core_so] = e.label;
  }

  const files = bios.files ?? [];
  const wantedFiles = files.filter((f) => f.wanted === "needed" || f.wanted === "optional");
  const countOf = (wanted: FirmwareWanted) => files.filter((f) => f.wanted === wanted).length;

  const fileElements = wantedFiles.map((f) => {
    const coreLines = f.cores ? buildBiosCoreLines(f.cores, coreLabelMap, coreInfo?.active_core) : [];

    return (
      <div key={f.file_name} className="romm-panel-file-row">
        <span key="dot" className="romm-status-dot" style={{ backgroundColor: fileDotColor(f) }} />
        <span key="name" className="romm-panel-file-name">
          {`${f.description || f.file_name}${fileNote(f)}`}
        </span>
        {coreLines.length > 0 ? (
          <div
            key="cores"
            style={{
              flexBasis: "100%",
              display: "flex",
              flexDirection: "column" as const,
              gap: "2px",
              marginLeft: "18px",
            }}
          >
            {coreLines}
          </div>
        ) : null}
      </div>
    );
  });

  for (const [wanted, phrase] of [
    ["not_needed", "no installed emulator asks for"],
    ["unknown", "nothing installed could answer for"],
  ] as const) {
    const count = countOf(wanted);
    if (count === 0) continue;
    fileElements.push(
      <div
        key={`${wanted}-note`}
        className="romm-panel-file-row"
        style={{ color: "rgba(255, 255, 255, 0.4)", fontSize: "12px", marginTop: "8px" }}
      >
        {`${count} file${count === 1 ? "" : "s"} on server ${phrase}`}
      </div>,
    );
  }

  return fileElements;
}

export const BiosTab: FC<BiosTabProps> = ({ biosStatus, biosLevel, coreInfo, isActive }) => {
  if (!isActive || !biosStatus) return null;

  // Left column: BIOS status + file list
  const biosColumn = buildBiosHeader(biosStatus, biosLevel);

  const fileElements = buildBiosFileList(biosStatus, coreInfo);
  if (fileElements.length > 0) {
    biosColumn.push(
      <div key="bios-file-list" className="romm-panel-file-list">
        {fileElements}
      </div>,
    );
  }

  // Right column: Core info
  const coreColumn = [
    <div key="core-title" className="romm-panel-section-title" style={{ marginBottom: "8px" }}>
      Emulator
    </div>,
    infoRow("core", "Active Core", coreInfo?.active_core_label ? coreInfo.active_core_label : "Default"),
  ];

  return section(
    "bios-core",
    null,
    <div key="bios-core-columns" style={{ display: "flex", gap: "24px" }}>
      <div key="bios-col" style={{ flex: 1, minWidth: 0 }}>
        {biosColumn}
      </div>
      <div key="core-col" style={{ flexShrink: 0, minWidth: "120px" }}>
        {coreColumn}
      </div>
    </div>,
  );
};
