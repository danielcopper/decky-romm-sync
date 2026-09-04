/**
 * Everything the Library page's Platforms tab knows and does: the three
 * list-shaped reads it joins per platform, the per-selection core read, and the
 * actions a platform's detail offers.
 *
 * It lives above the tab boundary because Steam's tabbed page renders only the
 * active tab and keys it by tab id, so switching tabs unmounts the content and
 * mounting it again would re-issue every read and thaw the list's frozen order.
 * The page holds the state; the tab and its detail render it.
 *
 * The three reads answer different questions and each covers a set the others
 * do not: `get_platforms` is RomM's platforms with ROMs (the list itself),
 * `get_firmware_status` the BIOS state of the platforms it can speak for, and
 * `get_registry_platforms` the ROMs bound to a Steam shortcut per platform —
 * which is also what "this platform has synced games" means here, and the only
 * one of the three that answers it for every platform in the list.
 *
 * Structure and vocabulary: `docs/architecture/qam-panel.md`, section Library.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import {
  countPlatformSaves,
  debugLog,
  deletePlatformBios,
  deletePlatformSaves,
  downloadAllFirmware,
  downloadPlatformFirmwareFile,
  downloadRequiredFirmware,
  getFirmwareStatus,
  getPlatforms,
  getRegistryPlatforms,
  getSystemCoreInfo,
  logWarn,
  removePlatformShortcuts,
  reportRemovalResults,
  savePlatformSync,
  setAllPlatformsSync,
  setSystemCore,
} from "../../api/backend";
import type { FirmwarePlatformExt, PlatformSyncSetting, SystemCoreInfo } from "../../types";
import { detach } from "../../utils/detach";
import { batchConfirmLaunchOptions } from "../../utils/launchOptionsReconcile";
import { clearPlatformCollection } from "../../utils/collections";
import {
  capturePruneLeaseAdmission,
  isPruneLeaseCancellation,
  isPruneLeaseCancelled,
  mountPruneLeaseOwner,
  releasePruneLeasesByOwner,
  withPruneLease,
} from "../../utils/pruneLease";
import { removeShortcutsPaced } from "../../utils/shortcutRemoval";
import { withTimeout } from "../../utils/withTimeout";

const LEASE_OWNER = "library-platforms";
const REMOVAL_REPORT_TIMEOUT_MS = 15000;

/** Which group of the detail a status line belongs under, so a failed core
 *  switch is not reported below the removal buttons. */
export type StatusScope = "core" | "bios" | "remove";

export interface DetailStatus {
  /** The platform the line is about. Moving through the list changes the
   *  detail under an action that is still running, so the line is bound to the
   *  platform it was produced for rather than cleared on every selection. */
  slug: string;
  scope: StatusScope;
  text: string;
}

/** One platform as the list and the detail read it — the three reads joined. */
export interface PlatformRow {
  id: number;
  slug: string;
  name: string;
  /** RomM's own ROM count for the platform. Distinct from `shortcutCount`,
   *  which is what reached Steam; the header line shows both. */
  romCount: number;
  syncEnabled: boolean;
  /** `null` while the firmware read is in flight, and for a platform that read
   *  has nothing to say about — the list then shows no dot and no number. */
  firmware: FirmwarePlatformExt | null;
  shortcutCount: number;
}

/** The list's two groups, computed once and kept while the page is open, so
 *  toggling a row never moves it out from under the focus. */
export interface FrozenGroups {
  synced: string[];
  available: string[];
}

/**
 * The core read's answer for one platform. `undefined` is "not read yet",
 * `null` is "the read failed" — a detail that showed a spinner forever would
 * claim the answer is still coming.
 */
export type CoreAnswer = SystemCoreInfo | null | undefined;

/**
 * How many save files a platform holds. `undefined` is "not read yet" and
 * `null` is "the read failed" — the same three-way answer the core read gives,
 * and for the same reason: the Delete save files button disables on a real
 * zero, so an unknown must not look like one.
 */
export type SaveCountAnswer = number | null | undefined;

export interface PlatformsPageState {
  rows: Map<string, PlatformRow>;
  groups: FrozenGroups | null;
  loading: boolean;
  /** The platform list itself could not be read — the tab has nothing to show. */
  failed: boolean;
  /** RomM is unreachable: the BIOS downloads are withdrawn, everything else stands. */
  serverOffline: boolean;
  selectedSlug: string | null;
  select: (slug: string) => void;
  coreFor: (slug: string) => CoreAnswer;
  saveCountFor: (slug: string) => SaveCountAnswer;
  status: DetailStatus | null;
  /**
   * The platform whose action is in flight, or `null`. Every action on every
   * platform disables while one is running — the prune lease and the firmware
   * re-read are page-wide, so two at once would contend — and the slug is what
   * lets a pane that is not the one acting say why its buttons are dead.
   */
  busySlug: string | null;
  /** Bound to its platform for the reason {@link DetailStatus} is: walking the
   *  list under a running removal must not show its progress on another
   *  platform's pane. */
  removalProgress: { slug: string; removed: number; total: number } | null;
  toggleSync: (row: PlatformRow, enabled: boolean) => void;
  setAllSync: (enabled: boolean) => void;
  changeCore: (slug: string, pickedLabel: string) => void;
  downloadRequired: (slug: string) => void;
  downloadAll: (slug: string) => void;
  downloadOne: (slug: string, fileName: string) => void;
  deleteBios: (slug: string) => void;
  removeShortcuts: (row: PlatformRow) => void;
  deleteSaves: (row: PlatformRow) => void;
}

/**
 * Tell an open game-detail page that this platform's firmware changed, so it
 * re-reads its BIOS requirement instead of leaving the pre-change one standing
 * (#939).
 *
 * Call it only when firmware actually changed. The event fans out to every
 * mounted panel and each one that matches the slug pays a live
 * `check_platform_bios` for it (#1082), so a run that moved no files must stay
 * silent rather than send an event no panel can act on.
 */
function announceBiosChange(platformSlug: string): void {
  globalThis.dispatchEvent(
    new CustomEvent("romm_data_changed", { detail: { type: "bios", platform_slug: platformSlug } }),
  );
}

/** Synced above Available, alphabetical inside each, taken from the payload
 *  rather than from live state so a later toggle cannot reorder it. */
function freezeGroups(platforms: PlatformSyncSetting[]): FrozenGroups {
  const byName = [...platforms].sort((a, b) => a.name.localeCompare(b.name));
  return {
    synced: byName.filter((p) => p.sync_enabled).map((p) => p.slug),
    available: byName.filter((p) => !p.sync_enabled).map((p) => p.slug),
  };
}

export function usePlatformsPage(): PlatformsPageState {
  const [platforms, setPlatforms] = useState<PlatformSyncSetting[]>([]);
  const [groups, setGroups] = useState<FrozenGroups | null>(null);
  const [loading, setLoading] = useState(true);
  const [failed, setFailed] = useState(false);
  const [firmware, setFirmware] = useState<Record<string, FirmwarePlatformExt>>({});
  const [serverOffline, setServerOffline] = useState(false);
  const [shortcutCounts, setShortcutCounts] = useState<Record<string, number>>({});
  const [selectedSlug, setSelectedSlug] = useState<string | null>(null);
  const [cores, setCores] = useState<Record<string, SystemCoreInfo | null>>({});
  const [saveCounts, setSaveCounts] = useState<Record<string, number | null>>({});
  const [status, setStatus] = useState<DetailStatus | null>(null);
  const [busySlug, setBusySlug] = useState<string | null>(null);
  const [removalProgress, setRemovalProgress] = useState<{ slug: string; removed: number; total: number } | null>(null);

  // Which slugs a core read has already been issued for. A ref, not state:
  // walking the list issues one read per row and the guard must hold within a
  // single render pass, before any answer has come back.
  const coreRequested = useRef<Set<string>>(new Set());
  const saveCountRequested = useRef<Set<string>>(new Set());

  const refreshFirmware = useCallback(async () => {
    try {
      const result = await getFirmwareStatus();
      if (!result.success) return;
      setServerOffline(result.server_offline ?? false);
      setFirmware(Object.fromEntries(result.platforms.map((p) => [p.platform_slug, p])));
    } catch (e) {
      logWarn(`Failed to read firmware status: ${e}`);
    }
  }, []);

  const refreshShortcutCounts = useCallback(async () => {
    try {
      const result = await getRegistryPlatforms();
      setShortcutCounts(Object.fromEntries(result.platforms.map((p) => [p.slug, p.count])));
    } catch (e) {
      logWarn(`Failed to read platform shortcut counts: ${e}`);
    }
  }, []);

  const loadCore = useCallback((slug: string) => {
    if (coreRequested.current.has(slug)) return;
    coreRequested.current.add(slug);
    getSystemCoreInfo(slug)
      .then((info) => setCores((prev) => ({ ...prev, [slug]: info })))
      .catch((e) => {
        logWarn(`Failed to read the core for ${slug}: ${e}`);
        setCores((prev) => ({ ...prev, [slug]: null }));
      });
  }, []);

  const loadSaveCount = useCallback((slug: string) => {
    if (saveCountRequested.current.has(slug)) return;
    saveCountRequested.current.add(slug);
    countPlatformSaves(slug)
      .then((result) => setSaveCounts((prev) => ({ ...prev, [slug]: result.count })))
      .catch((e) => {
        logWarn(`Failed to count the save files for ${slug}: ${e}`);
        setSaveCounts((prev) => ({ ...prev, [slug]: null }));
      });
  }, []);

  /** Ask again after a delete: the count the button showed is now spent. */
  const reloadSaveCount = useCallback(
    (slug: string) => {
      saveCountRequested.current.delete(slug);
      loadSaveCount(slug);
    },
    [loadSaveCount],
  );

  const reloadCore = useCallback(
    (slug: string) => {
      coreRequested.current.delete(slug);
      setCores((prev) => {
        const next = { ...prev };
        delete next[slug];
        return next;
      });
      loadCore(slug);
    },
    [loadCore],
  );

  useEffect(() => {
    mountPruneLeaseOwner(LEASE_OWNER);
    getPlatforms()
      .then((result) => {
        if (!result.success) {
          setFailed(true);
          return;
        }
        setPlatforms(result.platforms);
        const frozen = freezeGroups(result.platforms);
        setGroups(frozen);
        const first = frozen.synced[0] ?? frozen.available[0] ?? null;
        setSelectedSlug(first);
        if (first) {
          loadCore(first);
          loadSaveCount(first);
        }
      })
      .catch(() => setFailed(true))
      .finally(() => setLoading(false));
    // The BIOS state and the shortcut counts fill in beside the list rather
    // than gating it: the platform read is the only one the tab cannot render
    // without, and the other two are slower (a RomM listing, a machine-wide
    // firmware walk) than the one the user is waiting on.
    // eslint-disable-next-line react-hooks/set-state-in-effect -- initial async data loads on mount are the standard React pattern; the rule is overzealous here
    detach(refreshFirmware());
    detach(refreshShortcutCounts());
    return () => {
      detach(releasePruneLeasesByOwner(LEASE_OWNER));
    };
  }, [loadCore, loadSaveCount, refreshFirmware, refreshShortcutCounts]);

  const rows = new Map<string, PlatformRow>(
    platforms.map((p) => [
      p.slug,
      {
        id: p.id,
        slug: p.slug,
        name: p.name,
        romCount: p.rom_count,
        syncEnabled: p.sync_enabled,
        firmware: firmware[p.slug] ?? null,
        shortcutCount: shortcutCounts[p.slug] ?? 0,
      },
    ]),
  );

  const select = useCallback(
    (slug: string) => {
      setSelectedSlug(slug);
      loadCore(slug);
      loadSaveCount(slug);
    },
    [loadCore, loadSaveCount],
  );

  const coreFor = useCallback((slug: string): CoreAnswer => cores[slug], [cores]);
  const saveCountFor = useCallback((slug: string): SaveCountAnswer => saveCounts[slug], [saveCounts]);

  // Disable all has to be able to put the list back exactly as it was, which no
  // functional update can reconstruct. The snapshot is written in an effect
  // rather than during render, and is read only from an event handler — after
  // the commit that wrote it.
  const platformsRef = useRef<PlatformSyncSetting[]>([]);
  useEffect(() => {
    platformsRef.current = platforms;
  }, [platforms]);

  const toggleSync = useCallback((row: PlatformRow, enabled: boolean) => {
    const flip = (want: boolean) =>
      setPlatforms((prev) => prev.map((p) => (p.slug === row.slug ? { ...p, sync_enabled: want } : p)));
    flip(enabled);
    detach(
      savePlatformSync(row.id, enabled).catch(() => {
        flip(!enabled);
      }),
    );
  }, []);

  const setAllSync = useCallback((enabled: boolean) => {
    const previous = platformsRef.current;
    setPlatforms((prev) => prev.map((p) => ({ ...p, sync_enabled: enabled })));
    detach(
      setAllPlatformsSync(enabled).catch(() => {
        setPlatforms(previous);
      }),
    );
  }, []);

  const changeCore = useCallback(
    (slug: string, pickedLabel: string) => {
      const answer = cores[slug];
      // Picking the default-marked emulator clears the per-platform override
      // (empty label → follow the es_systems default); any other pins it.
      const defaultLabel = answer?.emulators.find((e) => e.is_default)?.label;
      const label = pickedLabel === defaultLabel ? "" : pickedLabel;
      detach(debugLog(`setSystemCore: slug=${slug} label=${label} (selected=${pickedLabel})`));
      detach(
        (async () => {
          try {
            const admission = capturePruneLeaseAdmission(LEASE_OWNER);
            const result = await setSystemCore(slug, label);
            detach(debugLog(`setSystemCore: result success=${result.success}`));
            if (!result.success) {
              // #1016's frontend half: the switch did not happen and the label
              // still names the old core, so say so rather than leaving the
              // detail looking as though the pick landed.
              setStatus({ slug, scope: "core", text: result.message ?? "Could not change the core" });
              return;
            }
            // Re-bake launch_options for every affected installed ROM on this
            // platform. The backend returns the fresh command per bound
            // shortcut; confirm-set each so existing shortcuts launch with the
            // new core. Bounded-concurrency batches, so a platform with many
            // ROMs does not serialize worst-case per-shortcut confirm-poll
            // timeouts.
            await withPruneLease(
              result.prune_lease_token,
              "setSystemCore",
              (signal) => batchConfirmLaunchOptions(result.rebake_items ?? [], "setSystemCore", signal),
              LEASE_OWNER,
              admission,
            );
            setStatus(null);
            reloadCore(slug);
            // A different core wants different BIOS files, so the table below
            // the picker is stale until the overview is read again.
            await refreshFirmware();
            globalThis.dispatchEvent(
              new CustomEvent("romm_data_changed", { detail: { type: "core_changed", platform_slug: slug } }),
            );
          } catch (e) {
            detach(debugLog(`setSystemCore: error: ${e}`));
            setStatus({ slug, scope: "core", text: "Could not change the core" });
          }
        })(),
      );
    },
    [cores, refreshFirmware, reloadCore],
  );

  const runDownload = useCallback(
    (slug: string, work: () => Promise<{ success: boolean; message?: string; downloaded?: number }>) => {
      setBusySlug(slug);
      setStatus({ slug, scope: "bios", text: "Downloading…" });
      detach(
        (async () => {
          try {
            const result = await work();
            setStatus({ slug, scope: "bios", text: result.message ?? (result.success ? "Done" : "Download failed") });
            if (result.success) {
              await refreshFirmware();
              if ((result.downloaded ?? 0) > 0) announceBiosChange(slug);
            }
          } catch (e) {
            setStatus({ slug, scope: "bios", text: `Download failed: ${e}` });
          } finally {
            setBusySlug(null);
          }
        })(),
      );
    },
    [refreshFirmware],
  );

  const downloadRequired = useCallback(
    (slug: string) => runDownload(slug, () => downloadRequiredFirmware(slug)),
    [runDownload],
  );
  const downloadAll = useCallback((slug: string) => runDownload(slug, () => downloadAllFirmware(slug)), [runDownload]);
  const downloadOne = useCallback(
    (slug: string, fileName: string) => runDownload(slug, () => downloadPlatformFirmwareFile(slug, fileName)),
    [runDownload],
  );

  const deleteBios = useCallback(
    (slug: string) => {
      setBusySlug(slug);
      detach(
        (async () => {
          try {
            const result = await deletePlatformBios(slug);
            setStatus({ slug, scope: "bios", text: result.message });
            if (result.success) {
              await refreshFirmware();
              announceBiosChange(slug);
            }
          } catch (e) {
            setStatus({ slug, scope: "bios", text: `Failed to delete BIOS files: ${e}` });
          } finally {
            setBusySlug(null);
          }
        })(),
      );
    },
    [refreshFirmware],
  );

  const removeShortcuts = useCallback(
    (row: PlatformRow) => {
      setBusySlug(row.slug);
      setStatus({ slug: row.slug, scope: "remove", text: `Removing ${row.name} shortcuts…` });
      const admission = capturePruneLeaseAdmission(LEASE_OWNER);
      detach(
        (async () => {
          try {
            const result = await removePlatformShortcuts(row.slug);
            // The @migration_blocked / @sync_active_blocked gates short-circuit
            // to { success: false, message, ... } with no app_ids/rom_ids —
            // surface that message instead of cosmetically reporting a removal.
            if (!result.success) {
              setStatus({ slug: row.slug, scope: "remove", text: result.message ?? "Failed to remove shortcuts" });
              return;
            }
            await withPruneLease(
              result.prune_lease_token,
              "Platform shortcut removal",
              async (signal) => {
                await removeShortcutsPaced(
                  result.app_ids ?? [],
                  (removed, total) => setRemovalProgress({ slug: row.slug, removed, total }),
                  signal,
                );
                if (isPruneLeaseCancelled(signal)) return;
                await clearPlatformCollection(result.platform_name || row.name, signal);
                if (isPruneLeaseCancelled(signal)) return;
                if (result.rom_ids?.length || result.prune_lease_token) {
                  await withTimeout(
                    reportRemovalResults(result.rom_ids ?? [], result.prune_lease_token ?? null),
                    REMOVAL_REPORT_TIMEOUT_MS,
                  );
                }
              },
              LEASE_OWNER,
              admission,
            );
            setStatus({
              slug: row.slug,
              scope: "remove",
              text: `Removed ${row.shortcutCount} ${row.name} game${row.shortcutCount === 1 ? "" : "s"}`,
            });
            await refreshShortcutCounts();
          } catch (e) {
            // Leaving the page cancels the removal continuation — the backend
            // removal already committed, so this is teardown, not a failure.
            if (isPruneLeaseCancellation(e, admission)) {
              logWarn(`Platform shortcut removal continuation was cancelled: ${e}`);
              return;
            }
            setStatus({ slug: row.slug, scope: "remove", text: "Failed to remove shortcuts" });
          } finally {
            setBusySlug(null);
            setRemovalProgress(null);
          }
        })(),
      );
    },
    [refreshShortcutCounts],
  );

  const deleteSaves = useCallback(
    (row: PlatformRow) => {
      setBusySlug(row.slug);
      setStatus({ slug: row.slug, scope: "remove", text: `Deleting ${row.name} saves…` });
      detach(
        (async () => {
          try {
            const result = await deletePlatformSaves(row.slug);
            setStatus({ slug: row.slug, scope: "remove", text: result.message });
            globalThis.dispatchEvent(new CustomEvent("romm_data_changed", { detail: { type: "save_sync" } }));
          } catch {
            setStatus({ slug: row.slug, scope: "remove", text: "Failed to delete saves" });
          } finally {
            setBusySlug(null);
            reloadSaveCount(row.slug);
          }
        })(),
      );
    },
    [reloadSaveCount],
  );

  return {
    rows,
    groups,
    loading,
    failed,
    serverOffline,
    selectedSlug,
    select,
    coreFor,
    saveCountFor,
    status,
    busySlug,
    removalProgress,
    toggleSync,
    setAllSync,
    changeCore,
    downloadRequired,
    downloadAll,
    downloadOne,
    deleteBios,
    removeShortcuts,
    deleteSaves,
  };
}
