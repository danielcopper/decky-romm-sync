import { FC } from "react";
import { ProgressBarWithInfo } from "@decky/ui";
import type { AccordionState, PlatformRow } from "../utils/syncAccordion";
import type { SyncProgress } from "../types";

/** Max platforms to show without truncation */
const MAX_VISIBLE = 8;

interface SyncAccordionProps {
  accordion: AccordionState;
  syncProgress: SyncProgress | null;
  formatEta: (seconds: number) => string;
}

// ── Row icon helpers ──────────────────────────────────────────

function statusIcon(status: PlatformRow["status"]): string {
  switch (status) {
    case "pending":
    case "fetching":
      return "\u25CB"; // ○
    case "applying":
      return "\u27F3"; // ⟳
    case "done":
      return "\u2713"; // ✓
    case "partial":
      return "\u2717"; // ✗
    case "error":
      return "\u2717"; // ✗
    default:
      return "\u25CB";
  }
}

function statusColor(status: PlatformRow["status"]): string {
  switch (status) {
    case "done":
      return "#4caf50"; // green
    case "applying":
      return "#fff";
    case "partial":
      return "#ff9800"; // orange
    case "error":
      return "#f44336"; // red
    default:
      return "rgba(255,255,255,0.45)";
  }
}

function rowOpacity(status: PlatformRow["status"]): number {
  return status === "pending" || status === "fetching" ? 0.45 : 1;
}

// ── Counter text (right side) ─────────────────────────────────

function counterText(row: PlatformRow): string {
  if (row.status === "applying") {
    return `${row.shortcutsProcessed}/${row.shortcutsTotal || row.romCount}`;
  }
  if (row.status === "partial") {
    return `${row.shortcutsProcessed}/${row.shortcutsTotal || row.romCount}`;
  }
  return row.romCount.toLocaleString();
}

// ── Truncation logic ──────────────────────────────────────────

interface VisibleSlice {
  rows: Array<PlatformRow & { originalIndex: number }>;
  collapsedBefore: number;
  collapsedAfter: number;
}

function computeVisibleSlice(platforms: PlatformRow[], activeIdx: number): VisibleSlice {
  if (platforms.length <= MAX_VISIBLE) {
    return {
      rows: platforms.map((p, i) => ({ ...p, originalIndex: i })),
      collapsedBefore: 0,
      collapsedAfter: 0,
    };
  }

  // Show: first completed + 1 nearest completed before active + active + 2 nearest pending after + last pending
  const indices = new Set<number>();

  // Always include active
  if (activeIdx >= 0) indices.add(activeIdx);

  // 2 completed before active
  let beforeCount = 0;
  for (let i = (activeIdx >= 0 ? activeIdx : platforms.length) - 1; i >= 0 && beforeCount < 2; i--) {
    if (platforms[i].status === "done" || platforms[i].status === "partial") {
      indices.add(i);
      beforeCount++;
    }
  }

  // 2 pending after active
  let afterCount = 0;
  for (let i = Math.max(activeIdx, 0) + 1; i < platforms.length && afterCount < 2; i++) {
    if (platforms[i].status === "pending" || platforms[i].status === "fetching") {
      indices.add(i);
      afterCount++;
    }
  }

  // If still under budget, fill from start and end
  if (indices.size < MAX_VISIBLE) {
    for (let i = 0; i < platforms.length && indices.size < MAX_VISIBLE; i++) {
      indices.add(i);
    }
  }
  if (indices.size < MAX_VISIBLE) {
    for (let i = platforms.length - 1; i >= 0 && indices.size < MAX_VISIBLE; i--) {
      indices.add(i);
    }
  }

  const sorted = Array.from(indices).sort((a, b) => a - b);
  const minVisible = sorted[0];
  const maxVisible = sorted[sorted.length - 1]; // NOSONAR

  return {
    rows: sorted.map((i) => ({ ...platforms[i], originalIndex: i })),
    collapsedBefore: minVisible,
    collapsedAfter: platforms.length - 1 - maxVisible,
  };
}

// ── CollapsedSummary row ──────────────────────────────────────

const CollapsedRow: FC<{ count: number; done: boolean }> = ({ count, done }) => (
  <div style={{
    display: "flex",
    alignItems: "center",
    gap: "8px",
    padding: "2px 0",
    opacity: 0.5,
    fontSize: "12px",
    color: "rgba(255,255,255,0.55)",
  }}>
    <span style={{ width: "16px", textAlign: "center", color: done ? "#4caf50" : "rgba(255,255,255,0.35)" }}>
      {done ? "\u2713" : "\u25CB"}
    </span>
    <span style={{ fontStyle: "italic" }}>
      {done ? `(${count} platforms complete)` : `(${count} more platforms)`}
    </span>
  </div>
);

// ── Expanded platform detail ──────────────────────────────────

const PlatformDetail: FC<{ row: PlatformRow; formatEta: (s: number) => string; etaSec?: number | null }> = ({
  row,
  formatEta,
  etaSec,
}) => {
  const pct =
    row.shortcutsTotal > 0
      ? (row.shortcutsProcessed / row.shortcutsTotal) * 100
      : 0;
  const isDraining = row.shortcutsProcessed >= row.shortcutsTotal && row.shortcutsTotal > 0;

  return (
    <div style={{
      paddingLeft: "24px",
      paddingBottom: "4px",
      display: "flex",
      flexDirection: "column",
      gap: "4px",
    }}>
      <ProgressBarWithInfo
        indeterminate={false}
        nProgress={pct}
        sOperationText=""
      />
      <div style={{ display: "flex", gap: "8px", alignItems: "flex-start" }}>
        {/* Cover art thumbnail */}
        {row.lastArtworkBase64 ? (
          <img
            src={`data:image/jpeg;base64,${row.lastArtworkBase64}`}
            alt="Cover art"
            style={{
              width: "40px",
              height: "56px",
              objectFit: "cover",
              borderRadius: "3px",
              flexShrink: 0,
            }}
          />
        ) : (
          <div style={{
            width: "40px",
            height: "56px",
            backgroundColor: "rgba(255,255,255,0.06)",
            borderRadius: "3px",
            flexShrink: 0,
          }} />
        )}
        <div style={{ display: "flex", flexDirection: "column", gap: "2px", minWidth: 0 }}>
          <div style={{
            fontSize: "12px",
            color: "rgba(255,255,255,0.75)",
            fontStyle: "italic",
            overflow: "hidden",
            textOverflow: "ellipsis",
            whiteSpace: "nowrap",
          }}>
            {isDraining
              ? `Finishing artwork (${row.shortcutsTotal - row.shortcutsProcessed} remaining)...`
              : row.currentGame || ""}
          </div>
          {etaSec != null && etaSec > 0 ? (
            <div style={{ fontSize: "11px", color: "rgba(255,255,255,0.4)" }}>
              ~{formatEta(etaSec)} remaining
            </div>
          ) : null}
        </div>
      </div>
    </div>
  );
};

// ── Main SyncAccordion component ──────────────────────────────

export const SyncAccordion: FC<SyncAccordionProps> = ({
  accordion,
  syncProgress,
  formatEta,
}) => {
  const { platforms, activePlatformIndex, collectionsProgress, removalsProgress } = accordion;
  const donePlatforms = platforms.filter((p) => p.status === "done" || p.status === "partial").length;

  const { rows, collapsedBefore, collapsedAfter } = computeVisibleSlice(platforms, activePlatformIndex);

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: "2px", marginTop: "6px" }}>
      {/* ── Collapsed "before" summary ── */}
      {collapsedBefore > 0 && (
        <CollapsedRow count={collapsedBefore} done />
      )}

      {/* ── Visible platform rows ── */}
      {rows.map((row) => (
        <div key={row.slug}>
          {/* Row header */}
          <div style={{
            display: "flex",
            alignItems: "center",
            gap: "8px",
            padding: "3px 0",
            opacity: rowOpacity(row.status),
          }}>
            <span style={{
              width: "16px",
              textAlign: "center",
              fontSize: "14px",
              color: statusColor(row.status),
              fontWeight: row.status === "applying" ? "bold" : "normal",
            }}>
              {statusIcon(row.status)}
            </span>
            <span style={{
              flex: 1,
              fontSize: "13px",
              color: row.status === "applying" ? "#fff" : "rgba(255,255,255,0.85)",
              fontWeight: row.status === "applying" ? 600 : 400,
              overflow: "hidden",
              textOverflow: "ellipsis",
              whiteSpace: "nowrap",
            }}>
              {row.name}
            </span>
            <span style={{
              fontSize: "12px",
              color: "rgba(255,255,255,0.45)",
              flexShrink: 0,
            }}>
              {counterText(row)}
            </span>
          </div>

          {/* Expanded detail for active platform */}
          {row.originalIndex === activePlatformIndex && row.status === "applying" && (
            <PlatformDetail
              row={row}
              formatEta={formatEta}
              etaSec={syncProgress?.etaSec}
            />
          )}
        </div>
      ))}

      {/* ── Collapsed "after" summary ── */}
      {collapsedAfter > 0 && (
        <CollapsedRow count={collapsedAfter} done={false} />
      )}

      {/* ── Collections progress (State 6) ── */}
      {collectionsProgress && collectionsProgress.total > 0 && (
        <div style={{
          marginTop: "6px",
          display: "flex",
          flexDirection: "column",
          gap: "4px",
        }}>
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline" }}>
            <div style={{ fontSize: "12px", fontWeight: 600, color: "rgba(255,255,255,0.85)" }}>
              Building collections...
            </div>
            <div style={{ fontSize: "11px", color: "rgba(255,255,255,0.45)" }}>
              {collectionsProgress.current}/{collectionsProgress.total}
            </div>
          </div>
          <ProgressBarWithInfo
            indeterminate={false}
            nProgress={(collectionsProgress.current / collectionsProgress.total) * 100}
            sOperationText=""
          />
          {collectionsProgress.label ? (
            <div style={{ fontSize: "12px", color: "rgba(255,255,255,0.55)", fontStyle: "italic" }}>
              {collectionsProgress.label}
            </div>
          ) : null}
        </div>
      )}

      {/* ── Removals progress (State 7) ── */}
      {removalsProgress && removalsProgress.total > 0 && (
        <div style={{
          marginTop: "6px",
          display: "flex",
          flexDirection: "column",
          gap: "4px",
        }}>
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline" }}>
            <div style={{ fontSize: "12px", fontWeight: 600, color: "rgba(255,255,255,0.85)" }}>
              Cleaning up...
            </div>
            <div style={{ fontSize: "11px", color: "rgba(255,255,255,0.45)" }}>
              {removalsProgress.current}/{removalsProgress.total}
            </div>
          </div>
          <ProgressBarWithInfo
            indeterminate={false}
            nProgress={(removalsProgress.current / removalsProgress.total) * 100}
            sOperationText=""
          />
        </div>
      )}

      {/* ── Footer: overall ETA + elapsed ── */}
      <div style={{
        display: "flex",
        justifyContent: "space-between",
        fontSize: "11px",
        color: "rgba(255,255,255,0.4)",
        marginTop: "4px",
        paddingTop: "4px",
        borderTop: "1px solid rgba(255,255,255,0.06)",
      }}>
        <span>
          {activePlatformIndex >= 0
            ? `${donePlatforms + 1} of ${platforms.length} platforms`
            : `${donePlatforms} of ${platforms.length} platforms`}
          {syncProgress?.etaSec != null && syncProgress.etaSec > 0
            ? ` \u00B7 ~${formatEta(syncProgress.etaSec)} remaining`
            : ""}
        </span>
        <span>
          {syncProgress?.elapsedSec != null && syncProgress.elapsedSec > 5
            ? `${formatEta(syncProgress.elapsedSec)} elapsed`
            : ""}
        </span>
      </div>
    </div>
  );
};
