/**
 * Renderer for a single tracked save file row in an active slot. Builds the
 * header (filename + size + status badge), info rows (last synced / updated /
 * server save / local path), and the conflict banner; no I/O, no state.
 */

import { createElement } from "react";
import { DialogButton } from "@decky/ui";
import type { SaveFileStatus, SyncConflict } from "../../types";
import { scrollFocusedToCenter } from "../../utils/scrollHelpers";
import { formatTimestamp } from "../../utils/formatters";
import { formatAttributionSegment, formatBytes, formatRelativeTime, pickLastSyncer, statusLabel } from "./helpers";

// Label column width — keeps values aligned vertically across rows
const LABEL_WIDTH = "88px";

/** Render a labeled info row (label column + value column) inside the tracked save block */
export function infoRow(
  key: string,
  label: string,
  value: ReturnType<typeof createElement> | string | null,
  valueColor = "#c7cdd3",
): ReturnType<typeof createElement> | null {
  if (value == null || value === "") return null;
  return createElement("div", {
    key,
    style: { display: "flex", alignItems: "flex-start", fontSize: "11px", marginTop: "2px" },
  },
    createElement("span", {
      style: { color: "#697075", width: LABEL_WIDTH, flexShrink: 0 },
    }, label),
    createElement("div", { style: { color: valueColor, flex: 1, minWidth: 0 } }, value),
  );
}

export function renderSaveFileRow(
  f: SaveFileStatus,
  conflict: SyncConflict | undefined,
  lastSyncCheckAt: string | null,
): ReturnType<typeof createElement> {
  const { color, label } = statusLabel(f.status, f.last_sync_at);
  const syncTime = lastSyncCheckAt || f.last_sync_at;
  const lastSyncer = pickLastSyncer(f.device_syncs);
  const conflictActive = f.status === "conflict" || !!conflict;

  // Header value pieces (right-aligned meta: size + status)
  const headerMeta: (ReturnType<typeof createElement> | null)[] = [];
  if (f.local_size != null) {
    headerMeta.push(createElement("span", {
      key: "size",
      style: { fontSize: "11px", color: "#8f98a0" },
    }, formatBytes(f.local_size)));
  }
  headerMeta.push(createElement("span", {
    key: "status",
    className: "romm-save-status-label",
    style: { color, fontSize: "11px", fontWeight: 600 },
  }, label));

  // Last synced value: "just now · <attribution> ✓" — see formatAttributionSegment
  const lastSyncedPieces: string[] = [syncTime ? (formatRelativeTime(syncTime) || "Never") : "Never"];
  const attrSegment = formatAttributionSegment(f.uploaded_by_us, lastSyncer?.device_name);
  if (attrSegment !== null) lastSyncedPieces.push(attrSegment);
  if (f.is_current === false) {
    lastSyncedPieces.push("Newer version available on server");
  }
  const lastSyncedValue = lastSyncedPieces.join(" · ");

  // Server save value — two lines: "#18 · retroarch-mgba" / "<server_file_name>"
  const serverValueLines: ReturnType<typeof createElement>[] = [];
  if (f.server_save_id != null) {
    const headerParts: string[] = [`#${f.server_save_id}`];
    if (f.server_emulator) headerParts.push(f.server_emulator);
    serverValueLines.push(createElement("div", {
      key: "srv-head",
      style: { color: "#c7cdd3" },
    }, headerParts.join(" · ")));
    if (f.server_file_name) {
      serverValueLines.push(createElement("div", {
        key: "srv-fn",
        style: { color: "#8f98a0", fontFamily: "monospace", wordBreak: "break-all" as const, marginTop: "1px" },
      }, f.server_file_name));
    }
  }

  return createElement(DialogButton, {
    key: f.filename,
    style: {
      background: "transparent",
      border: "none",
      padding: "8px 0",
      textAlign: "left" as const,
      width: "100%",
      cursor: "default",
      display: "block",
    },
    noFocusRing: false,
    onFocus: scrollFocusedToCenter,
  },
    // Header row: filename (left) + size + status badge (right)
    createElement("div", {
      style: {
        display: "flex",
        alignItems: "center",
        justifyContent: "space-between",
        gap: "8px",
        marginBottom: "4px",
      },
    },
      createElement("div", {
        style: {
          fontSize: "13px",
          color: "#dcdedf",
          fontWeight: 600,
          overflow: "hidden",
          textOverflow: "ellipsis",
          whiteSpace: "nowrap" as const,
          flex: 1,
          minWidth: 0,
        },
      }, f.filename),
      createElement("div", {
        style: { display: "flex", alignItems: "center", gap: "8px", flexShrink: 0 },
      }, ...headerMeta),
    ),

    // Conflict banner (prominent)
    conflictActive
      ? createElement("div", {
          style: { fontSize: "11px", color: "#d94126", fontWeight: 600, marginTop: "2px", marginBottom: "2px" },
        }, "Conflict detected — resolve from the sync action")
      : null,

    // Info rows
    infoRow("last-synced", "Last synced:", lastSyncedValue),
    infoRow(
      "last-updated",
      "Last updated:",
      f.server_updated_at ? formatTimestamp(f.server_updated_at) : null,
      "#8f98a0",
    ),
    serverValueLines.length > 0
      ? infoRow("server", "Server save:", createElement("div", {}, ...serverValueLines))
      : null,
    f.local_path
      ? infoRow(
          "path",
          "Local path:",
          createElement("span", {
            style: { fontFamily: "monospace", wordBreak: "break-all" as const },
          }, f.local_path),
          "#5a6066",
        )
      : null,
  );
}
