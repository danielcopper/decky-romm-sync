/**
 * VersionPicker — the game-detail "Version" control for a sibling group (#1297).
 *
 * Rendered as a compact icon trigger in the play-button section, immediately to
 * the right of DiscSelector (its structural twin, #865): a single DialogButton
 * that opens an anchored `showContextMenu` list of every version in the group.
 * Each row is
 * marked — the active version (✓ + tint), the default (the version the
 * resolution chain + Preferred-region setting would pick), downloaded versions,
 * and versions that exist on the server but aren't synced locally yet. Per-row
 * covers load lazily (only for synced versions), never eagerly during sync
 * (ADR-0021 / #1267).
 *
 * Selecting a version while the game is not downloaded rebinds the group's Steam
 * shortcut to it (appId-safe: the name/appId stay sticky) so the Download button
 * fetches exactly that version. Switching a *downloaded* game rebinds it too and
 * confirm-writes the target's launch command onto the shortcut (#1298); if the
 * currently-bound install has unsynced saves the backend soft-blocks and the
 * picker offers the sync-or-strand confirm. A single-version group renders
 * nothing (the null-gate pattern).
 */

import { useState, useEffect, useRef, FC, ReactNode } from "react";
import { toaster } from "@decky/api";
import { Menu, MenuItem, showContextMenu, DialogButton } from "@decky/ui";
import { FaChevronDown, FaCompactDisc, FaLayerGroup } from "react-icons/fa";
import {
  getVersionList,
  switchVersion,
  syncRomSaves,
  refreshSaveStatus,
  getArtworkBase64,
  invalidateCachedGameDetail,
  logError,
  logWarn,
} from "../api/backend";
import type { VersionList, VersionInfo, SwitchVersionSuccess } from "../api/backend";
import { setLaunchOptionsConfirmed } from "../utils/steamShortcuts";
import { showUnsyncedSavesModal } from "./UnsyncedSavesSwitchModal";
import { getEventTarget } from "../utils/events";
import { detach } from "../utils/detach";
import type { RommDataChangedDetail } from "../types/events";

interface VersionPickerProps {
  appId: number;
}

// Steam accent blue for the active version, neutral grey otherwise — the same
// palette DiscSelector uses so the two game-detail pickers read as one system.
const ACTIVE_ACCENT = "#59b6ff";
const NEUTRAL_GREY = "#dcdedf";

const BADGE_COLORS: Record<"accent" | "muted" | "good", { bg: string; fg: string }> = {
  accent: { bg: "rgba(89, 182, 255, 0.18)", fg: ACTIVE_ACCENT },
  good: { bg: "rgba(91, 163, 43, 0.22)", fg: "#7ac74f" },
  muted: { bg: "rgba(255, 255, 255, 0.10)", fg: "rgba(255, 255, 255, 0.55)" },
};

/** A small pill badge (Default / Downloaded / not synced) shown after a row's label. */
const Badge: FC<{ text: string; tone: "accent" | "muted" | "good" }> = ({ text, tone }) => {
  const { bg, fg } = BADGE_COLORS[tone];
  return (
    <span
      style={{
        marginLeft: "8px",
        padding: "1px 7px",
        borderRadius: "10px",
        fontSize: "11px",
        fontWeight: 600,
        backgroundColor: bg,
        color: fg,
      }}
    >
      {text}
    </span>
  );
};

export const VersionPicker: FC<VersionPickerProps> = ({ appId }) => {
  const [versionList, setVersionList] = useState<VersionList | null>(null);
  // rom_id -> cover base64 for synced versions, filled lazily once the list loads.
  const [covers, setCovers] = useState<Record<number, string>>({});
  const coversRequested = useRef<Set<number>>(new Set());

  // Initial load + refresh on a version switch (this or another surface). The
  // loader is defined inside the effect (shared with the event handler) so its
  // post-`await` setState never reads as a synchronous effect-body write.
  useEffect(() => {
    let cancelled = false;
    const load = async (): Promise<void> => {
      try {
        const result = await getVersionList(appId);
        if (!cancelled) setVersionList(result);
      } catch (e) {
        logError(`VersionPicker: getVersionList failed: ${e}`);
      }
    };
    detach(load());

    const onDataChanged = (e: Event) => {
      const detail = (e as CustomEvent<RommDataChangedDetail>).detail;
      if (detail.type === "version_switched" && detail.app_id === appId) {
        detach(load());
      }
    };
    globalThis.addEventListener("romm_data_changed", onDataChanged);
    return () => {
      cancelled = true;
      globalThis.removeEventListener("romm_data_changed", onDataChanged);
    };
  }, [appId]);

  // Lazily fetch covers for the synced versions once the list is known. Only
  // versions with a local row (synced) have artwork; server-only stubs skip it.
  // The `cancelled` guard drops an in-flight setState if the panel unmounts (or
  // the list changes) mid-fetch, matching the panel's fetch-helper pattern.
  useEffect(() => {
    const versions = versionList?.versions;
    if (!versions) return;
    let cancelled = false;
    for (const v of versions) {
      if (!v.synced || coversRequested.current.has(v.rom_id)) continue;
      coversRequested.current.add(v.rom_id);
      getArtworkBase64(v.rom_id)
        .then((result) => {
          if (!cancelled && result.base64) setCovers((prev) => ({ ...prev, [v.rom_id]: result.base64! }));
        })
        .catch(() => {});
    }
    return () => {
      cancelled = true;
    };
  }, [versionList]);

  // Apply a successful switch: confirm-write the target's launch command onto the
  // Steam shortcut (blank for an uninstalled target — intended, so the shortcut
  // never keeps the old version's command), then invalidate the cache and
  // broadcast the switch so sibling surfaces re-read the new binding.
  //
  // The backend rebind is ALREADY committed by the time we get here, so the
  // cache-invalidate + broadcast must always run — even if the launch-command
  // write fails or throws. A missed confirm only leaves the shortcut on a stale
  // command; it self-heals at the next startup/sync reconcile, so we warn and
  // nudge the user rather than reporting the whole switch as failed.
  const applySwitchSuccess = async (result: SwitchVersionSuccess): Promise<void> => {
    let confirmed = false;
    try {
      confirmed = await setLaunchOptionsConfirmed(result.app_id, result.launch_options);
    } catch (e) {
      logError(`VersionPicker: launch-options confirm threw for rom ${result.rom_id} (appId ${result.app_id}): ${e}`);
    }
    if (!confirmed) {
      logError(`VersionPicker: could not confirm launch options for rom ${result.rom_id} (appId ${result.app_id})`);
      toaster.toast({ title: "RomM Sync", body: "Switched — re-switch if launch fails" });
    }
    invalidateCachedGameDetail(appId);
    globalThis.dispatchEvent(
      new CustomEvent("romm_data_changed", {
        detail: { type: "version_switched", app_id: appId, rom_id: result.rom_id },
      }),
    );
  };

  // Sync the stranded version's saves, then retry the switch. Any failure —
  // sync failed, sync surfaced conflicts, or the retry blocked again — aborts
  // with a short toast and re-runs the save-status refresh so the conflict UI
  // surfaces through the normal save_status_updated loop.
  const syncThenSwitch = async (unsyncedRomId: number, target: VersionInfo): Promise<void> => {
    const abort = (body: string): void => {
      toaster.toast({ title: "RomM Sync", body });
      detach(
        refreshSaveStatus(unsyncedRomId).catch((e) =>
          logWarn(`VersionPicker: post-abort save-status refresh failed for rom ${unsyncedRomId}: ${e}`),
        ),
      );
    };
    try {
      const sync = await syncRomSaves(unsyncedRomId);
      if (!sync.success) {
        abort("Couldn't sync saves — try again");
        return;
      }
      if (sync.conflicts && sync.conflicts.length > 0) {
        abort("Resolve save conflicts first");
        return;
      }
      const retry = await switchVersion(appId, target.rom_id, false);
      if (retry.success) {
        await applySwitchSuccess(retry);
      } else if (retry.reason === "unsynced_saves") {
        // The sync ran but the version still reports drift (a partial upload or a
        // race) — say so instead of the generic "couldn't switch".
        abort("Saves still unsynced — try again");
      } else {
        abort("Couldn't switch versions");
      }
    } catch (e) {
      logError(`VersionPicker: sync-then-switch failed: ${e}`);
      abort("Couldn't sync saves — try again");
    }
  };

  const handleSwitch = async (target: VersionInfo): Promise<void> => {
    if (target.active) return;
    try {
      const result = await switchVersion(appId, target.rom_id, false);
      if (result.success) {
        await applySwitchSuccess(result);
        return;
      }
      if (result.reason === "unsynced_saves") {
        const choice = await showUnsyncedSavesModal({
          versionName: result.unsynced_version_name,
          serverReachable: result.server_reachable,
        });
        if (choice === "cancel") return;
        if (choice === "sync_and_switch") {
          await syncThenSwitch(result.unsynced_rom_id, target);
          return;
        }
        // "Switch anyway" — the override skips the stranding gate; strand the
        // saves on disk (they stay recoverable, they just won't sync until the
        // user switches back).
        const forced = await switchVersion(appId, target.rom_id, true);
        if (forced.success) {
          await applySwitchSuccess(forced);
        } else {
          toaster.toast({ title: "RomM Sync", body: forced.message || "Could not switch version" });
        }
        return;
      }
      toaster.toast({ title: "RomM Sync", body: result.message || "Could not switch version" });
    } catch (e) {
      logError(`VersionPicker: switchVersion failed: ${e}`);
      toaster.toast({ title: "RomM Sync", body: "Could not switch version" });
    }
  };

  // Single-version / unknown / unbound → render nothing (zero footprint).
  if (!versionList?.multi_version || !versionList.versions || versionList.versions.length === 0) return null;

  const versions = versionList.versions;
  const active = versions.find((v) => v.active);
  // Accent the trigger when the bound version isn't the group's natural default
  // (mirrors DiscSelector's "pinned ≠ default" accent) — an instant "this game is
  // on a non-default version" read; neutral when it is the default.
  const activeIsDefault = active?.is_default ?? false;

  const rowCover = (v: VersionInfo): ReactNode => {
    const base64 = covers[v.rom_id];
    if (base64) {
      return (
        <img
          alt=""
          src={`data:image/png;base64,${base64}`}
          style={{ width: "28px", height: "28px", borderRadius: "3px", objectFit: "cover", flexShrink: 0 }}
        />
      );
    }
    return <FaCompactDisc size={20} color={NEUTRAL_GREY} style={{ flexShrink: 0 }} />;
  };

  const openMenu = (e: MouseEvent): void => {
    showContextMenu(
      <Menu label="Version">
        {versions.map((v) => (
          <MenuItem key={v.rom_id} onClick={() => detach(handleSwitch(v))}>
            <span
              style={{
                display: "inline-flex",
                alignItems: "center",
                gap: "10px",
                color: v.active ? ACTIVE_ACCENT : undefined,
              }}
            >
              {rowCover(v)}
              <span>{v.label || v.name || String(v.rom_id)}</span>
              {v.is_default ? <Badge text="Default" tone="accent" /> : null}
              {v.installed ? <Badge text="Downloaded" tone="good" /> : null}
              {!v.synced ? <Badge text="not synced" tone="muted" /> : null}
              {v.active ? <span style={{ marginLeft: "6px", fontWeight: 700 }}>✓</span> : null}
            </span>
          </MenuItem>
        ))}
      </Menu>,
      getEventTarget(e),
    );
  };

  // A compact icon-only trigger (twin of DiscSelector) — a single DialogButton so
  // it is one natively-focusable, gamepad-reachable element inside the play-section
  // Focusable row. The verbose per-version detail lives in the anchored menu.
  return (
    <DialogButton className="romm-disc-btn" onClick={openMenu} aria-label="Version" title="Version">
      <FaLayerGroup size={20} color={activeIsDefault ? NEUTRAL_GREY : ACTIVE_ACCENT} />
      <FaChevronDown size={10} color="#cfd3d8" />
    </DialogButton>
  );
};
