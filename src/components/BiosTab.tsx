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
 * CSS classes prefixed with `romm-panel-` are injected separately by
 * styleInjector.
 */

import { FC, type ReactElement } from "react";
import type { BiosStatus, CoreInfo } from "../types";
import { biosColorForLevel } from "../utils/biosColor";
import { infoRow, section } from "./panelSection";

interface BiosTabProps {
  /** The platform's BIOS requirement, or null when its core needs none — in
   *  which case there is no tab to render. */
  biosStatus: BiosStatus | null;
  /** Backend-computed readiness classification driving the status-dot color;
   *  null whenever there is no requirement. */
  biosLevel: "ok" | "partial" | "missing" | "unmanaged" | null;
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

/** The header line: status dot plus the readiness phrasing this surface uses. */
function buildBiosHeader(bios: BiosStatus, biosLevel: BiosTabProps["biosLevel"]): ReactElement[] {
  const localCount = bios.local_count ?? 0;
  const serverCount = bios.server_count ?? 0;
  const reqCount = bios.required_count;
  const reqDone = bios.required_downloaded;

  // Color is sourced from the backend ok/partial/missing classification via the
  // shared helper — never re-derived here. The verbose phrasing below stays this
  // surface's own concern (per-surface wording).
  const biosColor = biosColorForLevel(biosLevel);
  let biosLabel: string;
  if (biosLevel === "unmanaged") {
    // No registry coverage — the plugin makes no readiness claim. Honest text
    // over the neutral grey dot, never a false "All ready".
    biosLabel = "Not managed by the plugin";
  } else if (reqCount != null && reqDone != null) {
    biosLabel =
      reqDone >= reqCount
        ? `All required ready (${localCount}/${serverCount})`
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

/** One row per known firmware file, plus the "files on server" note. */
function buildBiosFileList(bios: BiosStatus, coreInfo: CoreInfo | null): ReactElement[] {
  // Build core_so -> label lookup from the dedicated core-info path (#923).
  // Only libretro emulators carry a core_so (a standalone emulator has none),
  // so filter those in for the per-core BIOS lines.
  const coreLabelMap: Record<string, string> = {};
  for (const e of coreInfo?.emulators ?? []) {
    if (e.core_so) coreLabelMap[e.core_so] = e.label;
  }

  // Filter out unknown files (not in registry) — they're noise from the server
  const knownFiles = (bios.files ?? []).filter((f) => f.classification !== "unknown");
  const unknownCount = (bios.files ?? []).length - knownFiles.length;

  const fileElements = knownFiles.map((f) => {
    // Dot color logic:
    // Green: downloaded
    // Red: missing + required by current core
    // Orange: missing + required by another core (not current)
    // Grey: optional for current core or not used by any known core
    let dotColor: string;
    if (f.downloaded) {
      dotColor = "#5ba32b";
    } else if (f.used_by_active !== false && f.classification === "required") {
      dotColor = "#d94126";
    } else if (!f.used_by_active && f.cores) {
      const requiredByOther = Object.values(f.cores).some((c) => c.required);
      dotColor = requiredByOther ? "#d4a72c" : "#8f98a0";
    } else {
      dotColor = "#8f98a0";
    }

    // Build per-core lines
    const coreLines = f.cores ? buildBiosCoreLines(f.cores, coreLabelMap, coreInfo?.active_core) : [];

    return (
      <div key={f.file_name} className="romm-panel-file-row">
        <span key="dot" className="romm-status-dot" style={{ backgroundColor: dotColor }} />
        <span key="name" className="romm-panel-file-name">
          {f.description || f.file_name}
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

  // The "files on server" note is independent of knownFiles.length so it
  // survives the unmanaged case (every file unknown → no known files); there it
  // is the honest signal about what the server holds. When there are known
  // files it reads as a "+ N other files" footnote.
  if (unknownCount > 0) {
    const plural = unknownCount === 1 ? "" : "s";
    const unknownNote =
      knownFiles.length > 0
        ? `+ ${unknownCount} other file${plural} on server (not required by any known core)`
        : `${unknownCount} file${plural} on server the plugin doesn't recognise`;
    fileElements.push(
      <div
        key="unknown-note"
        className="romm-panel-file-row"
        style={{ color: "rgba(255, 255, 255, 0.4)", fontSize: "12px", marginTop: "8px" }}
      >
        {unknownNote}
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
