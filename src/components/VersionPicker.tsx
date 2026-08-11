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
 * covers load lazily from the per-ROM cover cache (cache-first fetchCoverBase64,
 * #1346), so each version shows its own art rather than the group's shared grid
 * cover; a not-yet-synced sibling downloads its cover once.
 *
 * A version RomM no longer serves stays listed as retained context, dimmed and
 * unswitchable. When local data for it exists, that row carries a trash
 * affordance and activating it opens the removed-game cleanup confirmation
 * scoped to that ROM — the row is the menu's focusable unit, so the action has
 * to live on it rather than in a nested button or a row of its own.
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
import { addEventListener, removeEventListener } from "@decky/api";
import { showToast } from "../utils/toast";
import { Menu, MenuItem, showContextMenu, DialogButton } from "@decky/ui";
import { FaChevronDown, FaCompactDisc, FaLayerGroup, FaTrash } from "react-icons/fa";
import {
  getVersionList,
  switchVersion,
  syncRomSaves,
  refreshSaveStatus,
  fetchCoverBase64,
  logError,
  logWarn,
} from "../api/backend";
import type {
  VersionList,
  VersionInfo,
  SwitchVersionSuccess,
  SwitchVersionFailure,
  SwitchVersionUnsyncedSaves,
} from "../api/backend";
import { reportServerReachable } from "../utils/connectionState";
import { applyCommittedVersionSwitch } from "../utils/versionSwitchApplication";
import { showUnsyncedSavesModal } from "./UnsyncedSavesSwitchModal";
import { getEventTarget } from "../utils/events";
import { setBoundVanished } from "../utils/vanishedBinding";
import { detach } from "../utils/detach";
import {
  capturePruneLeaseAdmission,
  isPruneLeaseAdmissionCurrent,
  isPruneLeaseCancellation,
  mountPruneLeaseOwner,
  releasePruneLeasesByOwner,
  type PruneLeaseAdmission,
} from "../utils/pruneLease";
import type { RommDataChangedDetail, RommRomUninstalledDetail } from "../types/events";
import type { DownloadCompleteEvent, DownloadFailedEvent } from "../types";
import { openRemovedGamesCleanupModal } from "./RemovedGamesCleanup";

interface VersionPickerProps {
  appId: number;
}

// Steam accent blue for the active version, neutral grey otherwise — the same
// palette DiscSelector uses so the two game-detail pickers read as one system.
const ACTIVE_ACCENT = "#59b6ff";
const NEUTRAL_GREY = "#dcdedf";

const reportVersionListReachability = (result: VersionList): void => {
  if (result.server_query_failed) {
    reportServerReachable(false);
  } else if (result.multi_version && !result.bound_vanished) {
    reportServerReachable(true);
  }
};

const BADGE_COLORS: Record<"accent" | "muted" | "good", { bg: string; fg: string }> = {
  accent: { bg: "rgba(89, 182, 255, 0.18)", fg: ACTIVE_ACCENT },
  good: { bg: "rgba(91, 163, 43, 0.22)", fg: "#7ac74f" },
  muted: { bg: "rgba(255, 255, 255, 0.10)", fg: "rgba(255, 255, 255, 0.55)" },
};

/** The label a row (or a singleton binding) carries once RomM 404s its exact id. */
const VANISHED_HINT = "No longer available on RomM";

/** Accessible name of the trash affordance that opens the cleanup confirmation. */
const REMOVE_LOCAL_DATA_LABEL = "Remove local data";

/**
 * The cleanup affordance, shown on a vanished row and on a vanished singleton
 * binding. Icon-only: it sits at the right edge of a row that already says why
 * the version is unusable, so a text label would only repeat that hint.
 *
 * Colour comes from the injected stylesheet, never a `color` prop: Steam
 * repaints a focused destructive MenuItem red, and an inline colour would
 * survive that and leave a red icon on a red row. `onMenuRow` opts into the
 * focused-state flip, which must not reach the singleton button (its focus
 * background stays dark).
 */
const RemoveLocalDataIcon: FC<{ onMenuRow?: boolean; style?: React.CSSProperties }> = ({ onMenuRow, style }) => (
  <FaTrash
    size={14}
    role="img"
    aria-label={REMOVE_LOCAL_DATA_LABEL}
    className={onMenuRow ? "romm-vanished-trash romm-vanished-trash-row" : "romm-vanished-trash"}
    style={style}
  />
);

/** The italic inline hint that explains why a version can't be selected. */
const AvailabilityHint: FC<{ text: string }> = ({ text }) => (
  <span style={{ marginLeft: "8px", fontSize: "11px", fontStyle: "italic", color: NEUTRAL_GREY }}>{text}</span>
);

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
  const leaseOwner = `version-picker:${appId}`;
  const [versionList, setVersionList] = useState<VersionList | null>(null);
  // In-flight switch guard (#1345 round-2 / E): a switch rebinds the shortcut and
  // then relies on the version_switched re-fetch to refresh the (now stale) list.
  // While a switch is running the trigger is disabled + shows a throbber and the
  // menu can't open, so a rapid second click can't act against the stale list
  // (the swallowed switch-back bug) or interleave two switches' confirm polls.
  const [switching, setSwitching] = useState(false);
  // rom_id -> cover base64 for every version, filled lazily once the list loads.
  const [covers, setCovers] = useState<Record<number, string>>({});
  const coversRequested = useRef<Set<number>>(new Set());
  // The group's member rom_ids from the last loaded list — lets the install-change
  // listeners below ignore events for other games without a fetch.
  const memberIdsRef = useRef<Set<number>>(new Set());
  const listRequestIdRef = useRef(0);
  const loadVersionListRef = useRef<{
    appId: number;
    load: (source?: "normal" | "vanished_refusal") => Promise<void>;
  } | null>(null);

  // Initial load + refresh on a version switch (this or another surface). The
  // effect owns the loader lifetime for this appId. All request sources share one
  // generation so only the latest completion can publish list/reachability state.
  useEffect(() => {
    mountPruneLeaseOwner(leaseOwner);
    let cancelled = false;
    const load = async (source: "normal" | "vanished_refusal" = "normal"): Promise<void> => {
      const requestId = ++listRequestIdRef.current;
      const isCurrent = (): boolean => !cancelled && requestId === listRequestIdRef.current;
      try {
        const result = await getVersionList(appId);
        if (!isCurrent()) return;
        // get_version_list touches the server for the sibling view (#1345): an
        // explicit server_query_failed means offline; a multi-version list that
        // loaded without failure proves the server is reachable. A bound-id 404
        // is an entity verdict, not a connection signal, so it feeds neither
        // direction into the global store. A single/unbound group carries no
        // reachability signal.
        reportVersionListReachability(result);
        // Publish the bound-id verdict for the play button, which sits beside
        // this picker and cannot see its state. Only a positive `bound_vanished`
        // is knowledge — a failed query reports false, so an offline session
        // never disables the download (#1570 F20).
        setBoundVanished(appId, result.bound_vanished);
        memberIdsRef.current = new Set((result.versions ?? []).map((v) => v.rom_id));
        setVersionList(result);
      } catch (e) {
        if (!isCurrent()) return;
        if (source === "vanished_refusal") {
          logWarn(`VersionPicker: version-vanished list refresh failed: ${e}`);
        } else {
          logError(`VersionPicker: getVersionList failed: ${e}`);
        }
      } finally {
        // The post-switch version_switched reload landing is the "switch fully
        // settled" signal — clear the in-flight guard here so the trigger
        // re-enables against a FRESH list, never a stale one (#1345 round-2 / E).
        // In the finally (not just on success) so a failed reload can't leave the
        // guard stuck; on the initial mount load switching is already false (no-op).
        if (source === "normal" && isCurrent()) setSwitching(false);
      }
    };
    loadVersionListRef.current = { appId, load };
    detach(load());

    const onDataChanged = (e: Event) => {
      const detail = (e as CustomEvent<RommDataChangedDetail>).detail;
      const switched = detail.type === "version_switched" && detail.app_id === appId;
      const pruned =
        detail.type === "rom_pruned" &&
        (detail.app_ids.includes(appId) || detail.rom_ids.some((romId) => memberIdsRef.current.has(romId)));
      if (switched || pruned) detach(load());
    };
    globalThis.addEventListener("romm_data_changed", onDataChanged);

    // A download or an uninstall changes a group member's Downloaded badge
    // WITHOUT a version switch — reload so the menu never shows a superseded
    // install state. download_failed matters too: the sibling supersede removes
    // the old install when the download STARTS, so a failed download has still
    // changed the on-disk picture.
    const onInstallChanged = (romId: number) => {
      if (memberIdsRef.current.has(romId)) detach(load());
    };
    const dlComplete = addEventListener<[DownloadCompleteEvent]>("download_complete", (evt) =>
      onInstallChanged(evt.rom_id),
    );
    const dlFailed = addEventListener<[DownloadFailedEvent]>("download_failed", (evt) => onInstallChanged(evt.rom_id));
    const onUninstalled = (e: Event) => onInstallChanged((e as CustomEvent<RommRomUninstalledDetail>).detail.rom_id);
    globalThis.addEventListener("romm_rom_uninstalled", onUninstalled);

    return () => {
      cancelled = true;
      detach(releasePruneLeasesByOwner(leaseOwner));
      if (loadVersionListRef.current?.load === load) loadVersionListRef.current = null;
      globalThis.removeEventListener("romm_data_changed", onDataChanged);
      removeEventListener("download_complete", dlComplete);
      removeEventListener("download_failed", dlFailed);
      globalThis.removeEventListener("romm_rom_uninstalled", onUninstalled);
    };
  }, [appId, leaseOwner]);

  // Lazily fetch a cover for every version once the list is known, via the
  // cache-first fetchCoverBase64 (#1346): a synced version resolves from the
  // per-ROM cover cache, and a not-yet-synced sibling downloads its cover from
  // RomM once (coversRequested dedupes). Loading here — on the list load, not on
  // menu open — is deliberate: showContextMenu renders a static element, so a
  // cover fetched after the menu opened would not appear until it reopened;
  // loading now means covers are ready by the first open, and cache-first keeps
  // repeat renders cheap. Rows still without art keep the FaCompactDisc fallback.
  // The `cancelled` guard drops an in-flight setState if the panel unmounts (or
  // the list changes) mid-fetch, matching the panel's fetch-helper pattern.
  useEffect(() => {
    const versions = versionList?.versions;
    if (!versions) return;
    let cancelled = false;
    for (const v of versions) {
      if (coversRequested.current.has(v.rom_id)) continue;
      coversRequested.current.add(v.rom_id);
      fetchCoverBase64(v.rom_id)
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
  const applySwitchSuccess = async (result: SwitchVersionSuccess, admission: PruneLeaseAdmission): Promise<void> => {
    const confirmed = await applyCommittedVersionSwitch(
      result,
      (romId, cover) => setCovers((prev) => ({ ...prev, [romId]: cover })),
      admission,
    );
    if (!confirmed) {
      showToast("Switched — re-switch if launch fails");
    }
  };

  const refreshAfterVanishedRefusal = (): Promise<void> => {
    const loader = loadVersionListRef.current;
    if (loader?.appId !== appId) return Promise.resolve();
    return loader.load("vanished_refusal");
  };

  const handleSwitchFailure = (result: SwitchVersionFailure | SwitchVersionUnsyncedSaves): void => {
    if (result.reason === "server_unreachable") reportServerReachable(false);
    setSwitching(false);
    showToast("Could not switch version", { subtext: result.message });
    if (result.reason === "version_vanished") detach(refreshAfterVanishedRefusal());
  };

  // Sync the stranded version's saves, then retry the switch. Every failure —
  // sync failed, sync surfaced conflicts, the retry blocked again, or the retry
  // was refused outright — ends the attempt with a toast and re-runs the
  // save-status refresh, so the conflict UI (or the just-completed upload)
  // surfaces through the normal save_status_updated loop.
  const syncThenSwitch = async (
    unsyncedRomId: number,
    target: VersionInfo,
    admission: PruneLeaseAdmission,
  ): Promise<void> => {
    // The SavesTab has no other refresh trigger — `refresh_save_status` is what
    // drives the `save_status_updated` chain — so EVERY terminal failure here
    // must run it, or the tab keeps showing pre-sync status until it is re-entered.
    const refreshStrandedSaveStatus = (): void => {
      detach(
        refreshSaveStatus(unsyncedRomId).catch((e) =>
          logWarn(`VersionPicker: post-abort save-status refresh failed for rom ${unsyncedRomId}: ${e}`),
        ),
      );
    };
    const abort = (body: string): void => {
      // Every sync-then-switch failure is terminal for this attempt — release the
      // in-flight guard so the trigger re-enables (it never reaches a reload).
      setSwitching(false);
      showToast(body);
      refreshStrandedSaveStatus();
    };
    if (!isPruneLeaseAdmissionCurrent(admission)) return;
    try {
      const sync = await syncRomSaves(unsyncedRomId);
      if (!isPruneLeaseAdmissionCurrent(admission)) return;
      if (!sync.success) {
        abort("Couldn't sync saves — try again");
        return;
      }
      if (sync.conflicts && sync.conflicts.length > 0) {
        abort("Resolve save conflicts first");
        return;
      }
      if (!isPruneLeaseAdmissionCurrent(admission)) return;
      const retry = await switchVersion(appId, target.rom_id, false);
      if (retry.success) {
        await applySwitchSuccess(retry, admission);
      } else if (retry.reason === "unsynced_saves") {
        // The sync ran but the version still reports drift (a partial upload or a
        // race) — say so instead of the generic "couldn't switch".
        abort("Saves still unsynced — try again");
      } else {
        // The sync landed but the retried switch was refused (e.g. the target
        // version vanished). handleSwitchFailure owns the toast + guard; the
        // refresh is still ours, since the saves DID move.
        handleSwitchFailure(retry);
        refreshStrandedSaveStatus();
      }
    } catch (e) {
      if (!isPruneLeaseAdmissionCurrent(admission)) return;
      logError(`VersionPicker: sync-then-switch failed: ${e}`);
      abort("Couldn't sync saves — try again");
    }
  };

  // Set the in-flight guard on entry and release it on every non-success terminal
  // path (below). On success we deliberately LEAVE it set: `applySwitchSuccess`
  // always broadcasts version_switched, and the resulting list reload clears the
  // guard once the fresh list lands — so the trigger never re-enables against a
  // stale list.
  /** Ask what to do about the saves the switch would strand, then do it. */
  const resolveUnsyncedSaves = async (
    result: SwitchVersionUnsyncedSaves,
    target: VersionInfo,
    admission: PruneLeaseAdmission,
  ): Promise<void> => {
    // The soft-block response carries a definitive reachability verdict (#1345).
    reportServerReachable(result.server_reachable);
    const choice = await showUnsyncedSavesModal({
      versionName: result.unsynced_version_name,
      serverReachable: result.server_reachable,
    });
    if (!isPruneLeaseAdmissionCurrent(admission)) return;
    if (choice === "cancel") {
      setSwitching(false);
      return;
    }
    if (choice === "sync_and_switch") {
      // syncThenSwitch owns the guard from here: it clears on abort and leaves
      // it set on its own success (its reload clears it).
      await syncThenSwitch(result.unsynced_rom_id, target, admission);
      return;
    }
    // "Switch anyway" — the override skips the stranding gate; strand the
    // saves on disk (they stay recoverable, they just won't sync until the
    // user switches back).
    const forced = await switchVersion(appId, target.rom_id, true);
    if (forced.success) {
      await applySwitchSuccess(forced, admission);
    } else {
      handleSwitchFailure(forced);
    }
  };

  const handleSwitch = async (target: VersionInfo): Promise<void> => {
    if (target.active || target.vanished) return;
    // A non-switchable row is a RomM sibling that lives in a different local group
    // (#1359) — its row is rendered disabled, and this guard makes a click a no-op
    // (defense-in-depth), so switch_version's rejection can never reach a toast.
    if (!target.switchable) return;
    setSwitching(true);
    const admission = capturePruneLeaseAdmission(leaseOwner);
    try {
      const result = await switchVersion(appId, target.rom_id, false);
      if (result.success) {
        await applySwitchSuccess(result, admission);
        return;
      }
      if (result.reason === "unsynced_saves") {
        await resolveUnsyncedSaves(result, target, admission);
        return;
      }
      // Keep the toast body short (Steam truncates it to one line) and put the
      // backend detail in the subtext so the reason is readable (#1359).
      handleSwitchFailure(result);
    } catch (e) {
      // A teardown-cancelled continuation is not a switch failure: the backend
      // rebind either committed or was never attempted, and this picker is gone.
      // Stay silent (and touch no state) rather than toast at the next surface.
      if (isPruneLeaseCancellation(e, admission)) {
        logWarn(`VersionPicker: version switch continuation was cancelled: ${e}`);
        return;
      }
      setSwitching(false);
      logError(`VersionPicker: switchVersion failed: ${e}`);
      showToast("Could not switch version");
    }
  };

  const openCleanup = (romId: number): void => {
    detach(
      openRemovedGamesCleanupModal(romId)
        .then((opened) => {
          if (!opened) showToast("This local entry already changed.");
        })
        .catch((error) => {
          logError(`VersionPicker: cleanup preview failed for rom ${romId}: ${error}`);
          showToast("Could not prepare local cleanup.");
        }),
    );
  };

  if (!versionList?.multi_version) {
    const bound = versionList?.bound_version;
    if (!versionList?.bound_vanished || !bound?.synced) return null;
    // A single-member group has nothing to pick between, so it renders no menu —
    // but its vanished binding still has to SAY why the game is unusable, next to
    // the inline cleanup that is the only action left for it.
    return (
      <span style={{ display: "inline-flex", alignItems: "center" }}>
        <DialogButton
          className="romm-disc-btn"
          onClick={() => openCleanup(bound.rom_id)}
          aria-label={REMOVE_LOCAL_DATA_LABEL}
          title={REMOVE_LOCAL_DATA_LABEL}
        >
          <RemoveLocalDataIcon />
        </DialogButton>
        <AvailabilityHint text={VANISHED_HINT} />
      </span>
    );
  }
  if (!versionList.versions || versionList.versions.length === 0) return null;

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

  const rowAvailabilityHint = (v: VersionInfo): ReactNode => {
    if (v.vanished) return <AvailabilityHint text={VANISHED_HINT} />;
    if (!v.switchable) return <AvailabilityHint text="conflicting metadata match in RomM" />;
    return null;
  };

  const openMenu = (e: MouseEvent): void => {
    // Blocked while a switch is in flight — the list is stale until the reload
    // lands, so opening it now would let a click act against the wrong versions.
    if (switching) return;
    showContextMenu(
      <Menu label="Version">
        {versions.map((v) => {
          // A synced vanished row has exactly one thing left to offer, so the row
          // IS that offer: it carries the trash affordance and activates the
          // cleanup confirmation. The action has to sit on the row itself — a
          // MenuItem is the menu's focusable unit, so a nested button would be
          // unreachable by gamepad and a row of its own belongs to no version
          // visually. Switching stays impossible either way: handleSwitch
          // refuses a vanished target.
          const removable = v.vanished && v.synced;
          return (
            <MenuItem
              key={v.rom_id}
              disabled={!removable && (v.vanished || !v.switchable)}
              {...(removable ? { tone: "destructive" as const } : {})}
              onClick={() => (removable ? openCleanup(v.rom_id) : detach(handleSwitch(v)))}
            >
              <span
                style={{
                  display: "inline-flex",
                  alignItems: "center",
                  gap: "10px",
                  width: "100%",
                  color: v.active ? ACTIVE_ACCENT : undefined,
                  // Unavailable/conflicting rows stay visible as retained context,
                  // but are dimmed until RomM offers a usable target.
                  opacity: v.vanished || !v.switchable ? 0.55 : undefined,
                }}
              >
                {rowCover(v)}
                <span>{v.label || v.name || String(v.rom_id)}</span>
                {v.is_default ? <Badge text="Default" tone="accent" /> : null}
                {v.installed ? <Badge text="Downloaded" tone="good" /> : null}
                {v.switchable && !v.synced ? <Badge text="not synced" tone="muted" /> : null}
                {rowAvailabilityHint(v)}
                {v.active ? <span style={{ marginLeft: "6px", fontWeight: 700 }}>✓</span> : null}
                {removable ? <RemoveLocalDataIcon onMenuRow style={{ marginLeft: "auto", flexShrink: 0 }} /> : null}
              </span>
            </MenuItem>
          );
        })}
      </Menu>,
      getEventTarget(e),
    );
  };

  // A compact icon-only trigger (twin of DiscSelector) — a single DialogButton so
  // it is one natively-focusable, gamepad-reachable element inside the play-section
  // Focusable row. The verbose per-version detail lives in the anchored menu.
  return (
    <DialogButton
      className="romm-disc-btn"
      onClick={openMenu}
      disabled={switching}
      aria-label="Version"
      title="Version"
      style={switching ? { opacity: 0.55 } : {}}
    >
      <FaLayerGroup size={20} color={activeIsDefault ? NEUTRAL_GREY : ACTIVE_ACCENT} />
      {switching ? (
        <span className="romm-throbber" style={{ width: "14px", height: "14px" }} />
      ) : (
        <FaChevronDown size={10} color="#cfd3d8" />
      )}
    </DialogButton>
  );
};
