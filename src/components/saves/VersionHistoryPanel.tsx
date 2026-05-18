/**
 * Expandable per-save version history sub-panel. Lazy-loads previous versions
 * when first expanded and drives the restore flow (with conflict pre-flight
 * fallback to the standard sync-conflict modal).
 */

import { useState, createElement, FC } from "react";
import { DialogButton } from "@decky/ui";
import { toaster } from "@decky/api";
import { debugLog, savesListFileVersions, savesRollbackToVersion } from "../../api/backend";
import type { SaveVersionEntry, RollbackStatus, ListFileVersionsResult } from "../../api/backend";
import { showSyncConflictModal } from "../SyncConflictModal";
import { scrollFocusedToCenter } from "../../utils/scrollHelpers";
import { formatTimestamp } from "../../utils/formatters";
import { formatAttributionSegment, formatBytes, formatRelativeTime, pickLastSyncer } from "./helpers";

interface VersionHistoryPanelProps {
  romId: number;
  slot: string;
  filename: string;
  isOffline: boolean;
  onRestored: () => void;
}

export const VersionHistoryPanel: FC<VersionHistoryPanelProps> = ({
  romId,
  slot,
  filename,
  isOffline,
  onRestored,
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
      if (result.status === "ok") {
        setVersions(result.versions);
      } else if (result.status === "server_unreachable") {
        debugLog(`VersionHistoryPanel: server unreachable for ${filename}: ${result.error}`);
        setVersions(null);
        setLoadError("Couldn't reach RomM. Tap retry.");
      }
    } catch (e) {
      debugLog(`VersionHistoryPanel: failed to load versions for ${filename}: ${e}`);
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
        if (first) await showSyncConflictModal(first);
        toaster.toast({ title: "RomM Sync", body: "Resolve the conflict, then try again" });
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
      } else if (result.status === "unsupported") {
        toaster.toast({ title: "RomM Sync", body: "Version history requires RomM 4.7+" });
      }
    } catch (e) {
      debugLog(`VersionHistoryPanel: restore error for save ${version.id}: ${e}`);
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

    return createElement("div", {
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
      createElement("div", { style: { flex: 1, minWidth: 0 } },
        // Line 1: #id · emulator · size
        createElement("div", {
          style: { fontSize: "12px", color: "#c7cdd3", fontWeight: 600 },
        }, headerParts.join(" · ")),
        // Line 2: last updated + device
        createElement("div", {
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
        createElement("div", {
          style: {
            fontSize: "11px",
            color: "#8f98a0",
            fontFamily: "monospace",
            wordBreak: "break-all" as const,
            marginTop: "2px",
          },
        }, v.file_name),
      ),
      // Restore button (fixed right, disabled when offline)
      createElement(DialogButton, {
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
        onClick: () => { handleRestore(v); },
      }, isThisRestoring ? "Restoring..." : "Restore"),
    );
  };

  const renderBody = (): ReturnType<typeof createElement> | ReturnType<typeof createElement>[] => {
    if (isOffline) {
      return createElement("div", {
        style: { fontSize: "11px", color: "#8f98a0", fontStyle: "italic" as const },
      }, "Offline — versions unavailable");
    }
    if (loading) {
      return createElement("div", { style: { fontSize: "11px", color: "#8f98a0" } }, "Loading...");
    }
    if (loadError !== null) {
      // Distinct from the empty-list case: surface a retry affordance so
      // the user isn't misled into thinking there are no versions when
      // the server was actually unreachable.
      return createElement("div", {
        style: { display: "flex", alignItems: "center", gap: "8px" },
      },
        createElement("span", {
          style: { fontSize: "11px", color: "#c46161", fontStyle: "italic" as const },
        }, loadError),
        createElement(DialogButton, {
          style: { padding: "2px 8px", minWidth: "auto", fontSize: "11px", width: "auto", flexShrink: 0 },
          noFocusRing: false,
          onFocus: scrollFocusedToCenter,
          onClick: () => { void loadVersions(); },
        }, "Retry"),
      );
    }
    if (versionCount === 0) {
      return createElement("div", {
        style: { fontSize: "11px", color: "#8f98a0", fontStyle: "italic" as const },
      }, "No older versions available");
    }
    return (versions ?? []).map(renderVersionRow);
  };

  return createElement("div", {
    key: `history-${filename}`,
    style: { marginTop: "4px", marginLeft: "8px" },
  },
    // Expander toggle
    createElement(DialogButton, {
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
      onClick: handleToggle,
    },
      createElement("span", {}, expanded ? "▾" : "▸"),
      createElement("span", {}, expanded && versions !== null
        ? `Previous Versions (${versionCount})`
        : "Previous Versions"),
    ),

    // Version list (lazy-loaded)
    expanded
      ? createElement("div", { style: { marginTop: "4px" } }, renderBody())
      : null,
  );
};
