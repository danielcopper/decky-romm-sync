/**
 * Expandable per-save version history sub-panel. Lazy-loads previous versions
 * when first expanded and drives the restore flow (with conflict pre-flight
 * fallback to the standard sync-conflict modal).
 */

import { useState, useEffect, createElement, FC } from "react";
import { DialogButton } from "@decky/ui";
import { toaster } from "@decky/api";
import { debugLog, savesListFileVersions, savesRollbackToVersion } from "../../api/backend";
import type { SaveVersionEntry, RollbackStatus, ListFileVersionsResult } from "../../types";
import { showSyncConflictModal } from "../SyncConflictModal";
import { scrollFocusedToCenter } from "../../utils/scrollHelpers";
import { formatBytes, formatTimestamp } from "../../utils/formatters";
import { formatAttributionSegment, formatRelativeTime, pickLastSyncer } from "./helpers";
import { renderCopyToSlotButton, type CopyToSlotHandler } from "./CopyToSlotButton";
import { detach } from "../../utils/detach";

interface VersionHistoryPanelProps {
  romId: number;
  slot: string;
  filename: string;
  isOffline: boolean;
  onRestored: () => void;
  /** Opens the copy-to-slot picker for a version row's save id (source = this slot). */
  onCopy?: CopyToSlotHandler;
}

export const VersionHistoryPanel: FC<VersionHistoryPanelProps> = ({
  romId,
  slot,
  filename,
  isOffline,
  onRestored,
  onCopy,
}) => {
  const [expanded, setExpanded] = useState(false);
  const [versions, setVersions] = useState<SaveVersionEntry[] | null>(null);
  const [loading, setLoading] = useState(false);
  const [restoring, setRestoring] = useState<number | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);

  const loadVersions = async () => {
    setLoading(true);
    setLoadError(null);
    try {
      const result: ListFileVersionsResult = await savesListFileVersions(romId, slot, filename);
      if (result.status === "ok" || result.status === "multi_file_unsupported") {
        // multi_file_unsupported carries an empty list — a multi-file slot's
        // siblings are components, not prior versions (#908). The panel is
        // hidden for multi-file slots anyway; this is the defensive backstop.
        setVersions(result.versions);
      } else {
        detach(debugLog(`VersionHistoryPanel: server unreachable for ${filename}: ${result.message}`));
        setVersions(null);
        setLoadError("Couldn't reach RomM. Tap retry.");
      }
    } catch (e) {
      detach(debugLog(`VersionHistoryPanel: failed to load versions for ${filename}: ${e}`));
      setVersions(null);
      setLoadError("Couldn't reach RomM. Tap retry.");
    } finally {
      setLoading(false);
    }
  };

  const handleToggle = async () => {
    const willExpand = !expanded;
    setExpanded(willExpand);
    if (willExpand && versions === null && loadError === null && !isOffline) {
      await loadVersions();
    }
  };

  // Self-refresh when this ROM's saves change. A copy into this slot (or a sync
  // / external rollback) adds or removes a version, but the list is cached on
  // first expand — without this it stays stale in-session until the game page is
  // re-entered. The signal is a DOM CustomEvent (`globalThis.dispatchEvent`),
  // NOT an @decky/api emit, so we listen on `globalThis`. Invalidate on any
  // change for this ROM; reload immediately when the panel is open, otherwise the
  // next expand lazy-loads the fresh list. `expanded` is a dep so the handler
  // always sees the live open/closed state (re-subscribing on toggle is cheap).
  useEffect(() => {
    const onDataChanged = (e: Event) => {
      const detail = (e as CustomEvent).detail;
      if (detail?.rom_id !== romId) return;
      setVersions(null);
      setLoadError(null);
      if (expanded && !isOffline) detach(loadVersions());
    };
    globalThis.addEventListener("romm_data_changed", onDataChanged);
    return () => globalThis.removeEventListener("romm_data_changed", onDataChanged);
    // eslint-disable-next-line react-hooks/exhaustive-deps -- loadVersions is stable per (romId, slot, filename); romId/isOffline/expanded are the real deps
  }, [romId, isOffline, expanded]);

  const handleRestore = async (version: SaveVersionEntry) => {
    setRestoring(version.id);
    try {
      const result: RollbackStatus = await savesRollbackToVersion(romId, slot, version.id);
      if (result.status === "ok") {
        toaster.toast({ title: "RomM Sync", body: `Save restored from ${formatRelativeTime(version.updated_at)}` });
        setVersions(null);
        setExpanded(false);
        onRestored();
      } else if (result.status === "conflict_blocked") {
        // Pre-flight surfaced a real conflict on the currently-tracked save.
        // The user has to resolve it via the standard sync conflict modal
        // before any switch can run. We surface the first conflict (in
        // practice the slot only ever has one); the modal itself is
        // identical to the one launched from the play button.
        const first = result.conflicts[0];
        if (first) {
          // The modal owns the feedback — Keep Local / Use Server surface the
          // normal save-sync resolution toast, Cancel stays silent (the state
          // remains conflict). The panel must not stack a second toast on top.
          await showSyncConflictModal(first);
        } else {
          // Degenerate: the server blocked on a conflict but sent none to
          // show, so there is no modal to surface it — nudge the user directly
          // instead of failing silently.
          toaster.toast({
            title: "RomM Sync",
            body: "Restore blocked by a sync conflict. Sync this save, then try again.",
          });
        }
      } else if (result.status === "preflight_failed") {
        const detail = result.errors[0] ?? "preflight error";
        toaster.toast({ title: "RomM Sync", body: `Sync failed before restore: ${detail}` });
      } else if (result.status === "put_failed") {
        // Local download succeeded but the server-side bump didn't — switch
        // is locally complete, just won't propagate to other devices yet.
        toaster.toast({
          title: "RomM Sync",
          body: "Restored locally, but the server didn't update. Other devices will see the previous version until you retry.",
        });
        setVersions(null);
        setExpanded(false);
        onRestored();
      } else if (result.status === "rom_not_installed") {
        // Distinct from ``version_deleted``: the chosen version may well
        // still exist on the server; the local ROM install is what's gone
        // (uninstalled between version-list load and restore tap).
        toaster.toast({ title: "RomM Sync", body: "ROM is no longer installed locally. Reinstall and try again." });
      } else if (result.status === "version_deleted") {
        toaster.toast({ title: "RomM Sync", body: "This version no longer exists on the server" });
      } else if (result.status === "server_unreachable") {
        // Distinct from ``not_found``: the version may well still exist;
        // we just couldn't reach the server to confirm. Prompt for retry
        // instead of telling the user the version is gone.
        toaster.toast({ title: "RomM Sync", body: "Couldn't reach RomM. Check your connection and try again." });
        // eslint-disable-next-line @typescript-eslint/no-unnecessary-condition -- exhaustive final branch of the 8-member RollbackStatus union; an explicit check (vs. plain `else`) keeps the per-status symmetry and leaves any future-added status unhandled instead of silently routing it to the "unsupported" toast
      } else if (result.status === "unsupported") {
        toaster.toast({ title: "RomM Sync", body: "Version history requires RomM 4.7+" });
      }
    } catch (e) {
      detach(debugLog(`VersionHistoryPanel: restore error for save ${version.id}: ${e}`));
    } finally {
      setRestoring(null);
    }
  };

  const versionCount = versions?.length ?? 0;

  const renderVersionRow = (v: SaveVersionEntry): ReturnType<typeof createElement> => {
    const lastSyncer = pickLastSyncer(v.device_syncs);
    const deviceName = lastSyncer?.device_name ?? null;
    const isThisRestoring = restoring === v.id;

    // Line 1: #id · emulator · size
    const headerParts: string[] = [`#${v.id}`];
    if (v.emulator) headerParts.push(v.emulator);
    if (v.file_size_bytes != null) headerParts.push(formatBytes(v.file_size_bytes));

    // Line 2: Last updated: <timestamp>[ · <device label> ✓]  — see formatAttributionSegment
    const lastUpdatedParts: string[] = [formatTimestamp(v.updated_at)];
    const attrSegment = formatAttributionSegment(v.uploaded_by_us, deviceName);
    if (attrSegment !== null) lastUpdatedParts.push(attrSegment);

    return createElement(
      "div",
      {
        key: `ver-${v.id}`,
        style: {
          display: "flex",
          alignItems: "flex-start",
          gap: "8px",
          padding: "6px 0",
          borderBottom: "1px solid rgba(255,255,255,0.06)",
        },
      },
      // Info column (grows)
      createElement(
        "div",
        { style: { flex: 1, minWidth: 0 } },
        // Line 1: #id · emulator · size
        createElement(
          "div",
          {
            style: { fontSize: "12px", color: "#c7cdd3", fontWeight: 600 },
          },
          headerParts.join(" · "),
        ),
        // Line 2: last updated + device
        createElement(
          "div",
          {
            style: {
              fontSize: "11px",
              color: "#8f98a0",
              marginTop: "2px",
            },
          },
          createElement("span", { style: { color: "#697075" } }, "Last updated: "),
          lastUpdatedParts.join(" · "),
        ),
        // Line 3: server filename (technical, bottom)
        createElement(
          "div",
          {
            style: {
              fontSize: "11px",
              color: "#8f98a0",
              fontFamily: "monospace",
              wordBreak: "break-all" as const,
              marginTop: "2px",
            },
          },
          v.file_name,
        ),
      ),
      // Action column (fixed right): Restore + optional Copy-to-slot.
      createElement(
        "div",
        { style: { display: "flex", flexDirection: "column" as const, gap: "4px", flexShrink: 0 } },
        // Restore button (disabled when offline)
        createElement(
          DialogButton,
          {
            key: "restore",
            style: {
              padding: "2px 8px",
              minWidth: "auto",
              fontSize: "11px",
              width: "auto",
              flexShrink: 0,
            },
            noFocusRing: false,
            onFocus: scrollFocusedToCenter,
            disabled: isThisRestoring || restoring !== null || isOffline,
            onClick: () => {
              detach(handleRestore(v));
            },
          },
          isThisRestoring ? "Restoring..." : "Restore",
        ),
        // Copy-to-slot button (source = this slot); disabled offline via the shared helper.
        onCopy ? renderCopyToSlotButton(`copy-ver-${v.id}`, v.id, { onCopy, sourceSlot: slot, isOffline }) : null,
      ),
    );
  };

  const renderBody = (): ReturnType<typeof createElement> | ReturnType<typeof createElement>[] => {
    if (isOffline) {
      return createElement(
        "div",
        {
          style: { fontSize: "11px", color: "#8f98a0", fontStyle: "italic" as const },
        },
        "Offline — versions unavailable",
      );
    }
    if (loading) {
      return createElement("div", { style: { fontSize: "11px", color: "#8f98a0" } }, "Loading...");
    }
    if (loadError !== null) {
      // Distinct from the empty-list case: surface a retry affordance so
      // the user isn't misled into thinking there are no versions when
      // the server was actually unreachable.
      return createElement(
        "div",
        {
          style: { display: "flex", alignItems: "center", gap: "8px" },
        },
        createElement(
          "span",
          {
            style: { fontSize: "11px", color: "#c46161", fontStyle: "italic" as const },
          },
          loadError,
        ),
        createElement(
          DialogButton,
          {
            style: { padding: "2px 8px", minWidth: "auto", fontSize: "11px", width: "auto", flexShrink: 0 },
            noFocusRing: false,
            onFocus: scrollFocusedToCenter,
            onClick: () => {
              detach(loadVersions());
            },
          },
          "Retry",
        ),
      );
    }
    if (versionCount === 0) {
      return createElement(
        "div",
        {
          style: { fontSize: "11px", color: "#8f98a0", fontStyle: "italic" as const },
        },
        "No older versions available",
      );
    }
    return (versions ?? []).map(renderVersionRow);
  };

  return createElement(
    "div",
    {
      key: `history-${filename}`,
      style: { marginTop: "4px", marginLeft: "8px" },
    },
    // Expander toggle
    createElement(
      DialogButton,
      {
        style: {
          background: "transparent",
          border: "none",
          padding: "2px 0",
          textAlign: "left" as const,
          width: "100%",
          cursor: "pointer",
          display: "flex",
          alignItems: "center",
          gap: "4px",
          fontSize: "11px",
          color: "#8f98a0",
        },
        noFocusRing: false,
        onFocus: scrollFocusedToCenter,
        onClick: () => {
          detach(handleToggle());
        },
      },
      createElement("span", {}, expanded ? "▾" : "▸"),
      createElement(
        "span",
        {},
        expanded && versions !== null ? `Previous Versions (${versionCount})` : "Previous Versions",
      ),
    ),

    // Version list (lazy-loaded)
    expanded ? createElement("div", { style: { marginTop: "4px" } }, renderBody()) : null,
  );
};
