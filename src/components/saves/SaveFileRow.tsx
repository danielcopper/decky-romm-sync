/**
 * Renderer for a single tracked save file row in an active slot. Builds the
 * header (filename + size + status badge), info rows (last synced / updated /
 * server save / local path), and the conflict banner; no I/O, no state.
 */

import type { ReactElement } from "react";
import { DialogButton } from "@decky/ui";
import type { SaveFileStatus, SyncConflict } from "../../types";
import { scrollFocusedToCenter } from "../../utils/scrollHelpers";
import { formatBytes, formatTimestamp } from "../../utils/formatters";
import { formatAttributionSegment, formatRelativeTime, pickLastSyncer, statusLabel } from "./helpers";

// Label column width — keeps values aligned vertically across rows
const LABEL_WIDTH = "88px";

/** Render a labeled info row (label column + value column) inside the tracked save block */
export function infoRow(
  key: string,
  label: string,
  value: ReactElement | string | null,
  valueColor = "#c7cdd3",
): ReactElement | null {
  if (value == null || value === "") return null;
  return (
    <div key={key} style={{ display: "flex", alignItems: "flex-start", fontSize: "11px", marginTop: "2px" }}>
      <span style={{ color: "#697075", width: LABEL_WIDTH, flexShrink: 0 }}>{label}</span>
      <div style={{ color: valueColor, flex: 1, minWidth: 0 }}>{value}</div>
    </div>
  );
}

/**
 * Compose the "Last synced" info-row value: "<time> · <attribution> ✓ · <stale hint>".
 *
 * The time binds to the last SUCCESS (`f.last_sync_at`), never the attempt time,
 * and the ✓ renders only for an at-rest file — a failed post-exit sync shows its
 * old success time with no ✓ (#1334). Appends the server-attribution segment and,
 * when the server has moved past us, the "newer version" hint.
 */
function buildLastSyncedValue(f: SaveFileStatus, isSynced: boolean): string {
  const lastSyncer = pickLastSyncer(f.device_syncs);
  const pieces: string[] = [f.last_sync_at ? formatRelativeTime(f.last_sync_at) || "Never" : "Never"];
  const attrSegment = formatAttributionSegment(f.uploaded_by_us, lastSyncer?.device_name, isSynced);
  if (attrSegment !== null) pieces.push(attrSegment);
  if (f.is_current === false) {
    pieces.push("Newer version available on server");
  }
  return pieces.join(" · ");
}

export function renderSaveFileRow(
  f: SaveFileStatus,
  conflict: SyncConflict | undefined,
  lastSyncCheckAt: string | null,
): ReactElement {
  const { color, label } = statusLabel(f.status, f.last_sync_at);
  // "synced"/"skip" are the at-rest states; anything else (upload/download/
  // conflict/unknown) has work pending, so the row must not claim a successful
  // sync — no ✓, and the "Checked" hint surfaces the attempt time instead (#1334).
  const isSynced = f.status === "synced" || f.status === "skip";
  const conflictActive = f.status === "conflict" || !!conflict;
  // The slot's most recent sync ATTEMPT — surfaced as a separate "Checked" hint
  // only when this file isn't at rest, so a pending/failed file shows both the
  // last success and the recent check without duplicating the synced ✓ line.
  const checkedHint = !isSynced && lastSyncCheckAt ? formatRelativeTime(lastSyncCheckAt) : null;

  // Header value pieces (right-aligned meta: size + status)
  const headerMeta: (ReactElement | null)[] = [];
  if (f.local_size != null) {
    headerMeta.push(
      <span key="size" style={{ fontSize: "11px", color: "#8f98a0" }}>
        {formatBytes(f.local_size)}
      </span>,
    );
  }
  headerMeta.push(
    <span key="status" className="romm-save-status-label" style={{ color, fontSize: "11px", fontWeight: 600 }}>
      {label}
    </span>,
  );

  // Last synced value: "just now · <attribution> ✓" — see buildLastSyncedValue
  const lastSyncedValue = buildLastSyncedValue(f, isSynced);

  // Server save value — two lines: "#18 · retroarch-mgba" / "<server_file_name>"
  const serverValueLines: ReactElement[] = [];
  if (f.server_save_id != null) {
    const headerParts: string[] = [`#${f.server_save_id}`];
    if (f.server_emulator) headerParts.push(f.server_emulator);
    serverValueLines.push(
      <div key="srv-head" style={{ color: "#c7cdd3" }}>
        {headerParts.join(" · ")}
      </div>,
    );
    if (f.server_file_name) {
      serverValueLines.push(
        <div
          key="srv-fn"
          style={{ color: "#8f98a0", fontFamily: "monospace", wordBreak: "break-all" as const, marginTop: "1px" }}
        >
          {f.server_file_name}
        </div>,
      );
    }
  }

  return (
    <DialogButton
      key={f.filename}
      style={{
        background: "transparent",
        border: "none",
        padding: "8px 0",
        textAlign: "left" as const,
        width: "100%",
        cursor: "default",
        display: "block",
      }}
      noFocusRing={false}
      onFocus={scrollFocusedToCenter}
    >
      {/* Header row: filename (left) + size + status badge (right) */}
      <div
        style={{
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
          gap: "8px",
          marginBottom: "4px",
        }}
      >
        <div
          style={{
            fontSize: "13px",
            color: "#dcdedf",
            fontWeight: 600,
            overflow: "hidden",
            textOverflow: "ellipsis",
            whiteSpace: "nowrap" as const,
            flex: 1,
            minWidth: 0,
          }}
        >
          {f.filename}
        </div>
        <div style={{ display: "flex", alignItems: "center", gap: "8px", flexShrink: 0 }}>{headerMeta}</div>
      </div>

      {/* Conflict banner (prominent) */}
      {conflictActive ? (
        <div style={{ fontSize: "11px", color: "#d94126", fontWeight: 600, marginTop: "2px", marginBottom: "2px" }}>
          Conflict detected — resolve from the sync action
        </div>
      ) : null}

      {/* Info rows */}
      {infoRow("last-synced", "Last synced:", lastSyncedValue)}
      {checkedHint ? infoRow("checked", "Checked:", checkedHint, "#8f98a0") : null}
      {infoRow(
        "last-updated",
        "Last updated:",
        f.server_updated_at ? formatTimestamp(f.server_updated_at) : null,
        "#8f98a0",
      )}
      {serverValueLines.length > 0 ? infoRow("server", "Server save:", <div>{serverValueLines}</div>) : null}
      {f.local_path
        ? infoRow(
            "path",
            "Local path:",
            <span style={{ fontFamily: "monospace", wordBreak: "break-all" as const }}>{f.local_path}</span>,
            "#5a6066",
          )
        : null}
    </DialogButton>
  );
}
