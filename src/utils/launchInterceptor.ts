/**
 * Global launch watcher (ADR-0015 — the "full funnel").
 *
 * Every gaming-mode launch of a RomM-owned shortcut that did NOT originate from
 * our Play button is intercepted here. Because no Steam hook can pause a launch,
 * run async work, and then proceed, the watcher uses the cancel-then-relaunch
 * mechanism: it `CancelGameAction`s the launch IMMEDIATELY (synchronously, which
 * wins the race against the un-pausable launch), runs the shared
 * {@link runLaunchGate} funnel, and on approval relaunches via `RunGame`.
 *
 * The one-shot skip-set (`markLaunchSkipped` / `consumeLaunchSkip`, owned by
 * `launchGate.ts`) exempts exactly one launch: the watcher's own relaunch and
 * the Play button's gated launch — so neither gets re-gated (no double-gate).
 *
 * Registered on plugin load, unregistered on unload.
 */

import { toaster } from "@decky/api";
import { isRomMAppId } from "../patches/gameDetailPatch";
import {
  refreshMigrationState,
  getInstalledRom,
  getCachedGameDetail,
  isSaveTrackingConfigured,
  getSaveSetupInfo,
  confirmSlotChoice,
  checkCoreChange,
  probeReachability,
  preLaunchSync,
  checkLocalDrift,
  logInfo,
  logError,
} from "../api/backend";
import { getMigrationState, setMigrationStatus } from "./migrationStore";
import { setSaveSortMigrationStatus } from "./saveSortMigrationStore";
import { getAppIdRomIdMapSnapshot } from "./sessionManager";
import { runLaunchGate, markLaunchSkipped, consumeLaunchSkip } from "./launchGate";
import type { GateVerdict, LaunchGateOps, PreLaunchSyncOutcome } from "./launchGate";
import { reconfirmLaunchOptions } from "./launchOptionsReconcile";
import { applyLaunchGateSetupOutcome, resolveSaveSetupOutcome } from "./saveSetup";
import { showCoreChangeModal } from "../components/CoreChangeModal";
import { handleConflicts } from "../components/SyncConflictModal";
import { showOfflineDriftModal } from "../components/OfflineDriftModal";
import { showFallbackLaunchModal } from "../components/FallbackLaunchModal";
import { SAVEFILES_IN_CONTENT_DIR_REASON } from "../types";
import { detach } from "./detach";

let gameActionHook: { unregister: () => void } | null = null;

/** Migration block copy — surfaced as a toast (no relaunch). */
const MIGRATION_TOAST_BODY = "Pending RetroDECK migration. Open the plugin QAM to migrate or dismiss.";

/**
 * Watcher variant of the tracking-setup gate. On a cold grid launch there is no
 * plugin page open, so — unlike the Play button — this MUST NOT route the user
 * to the saves tab. Instead it silently auto-adopts the default/recommended
 * slot (via `confirmSlotChoice`) and proceeds. A direct launch is never blocked
 * on setup — it always proceeds (the gate op below maps this to "proceed"); any
 * failure (server unreachable, needs-user-choice, a thrown error) is swallowed.
 */
async function ensureTrackingConfiguredWatcher(romId: number): Promise<void> {
  const trackingResult = await isSaveTrackingConfigured(romId).catch((e) => {
    logError(`Watcher tracking check failed (assuming configured): ${e}`);
    return { configured: true };
  });
  if (trackingResult.configured) return;

  let setupInfo;
  try {
    setupInfo = await getSaveSetupInfo(romId);
  } catch (e) {
    // Network/backend failure — never block a direct launch on setup.
    logError(`Watcher save-setup fetch failed (proceeding unconfigured): ${e}`);
    return;
  }

  // Reuse the shared outcome handler with a no-op saves-tab dispatch and a
  // swallowed toast: the auto_confirm branch fires `confirmSlotChoice`; every
  // other (abort) branch is irrelevant here because the watcher never aborts a
  // direct launch on tracking — an unconfigured slot the user can't resolve on
  // a cold launch must still let the game start (the next plugin-page visit
  // configures it).
  await applyLaunchGateSetupOutcome(resolveSaveSetupOutcome(setupInfo), {
    rid: romId,
    confirmSlotChoice,
    toast: () => undefined,
    dispatchSavesTab: () => undefined,
  }).catch((e) => logError(`Watcher auto-adopt slot failed (proceeding): ${e}`));
}

/**
 * Core-change gate — reuses the same check + imperative modal the Play button
 * uses. `showModal` works from this non-component context. Returns `true` to
 * proceed, `false` when the user cancelled.
 */
async function checkCoreChangeWatcher(romId: number): Promise<boolean> {
  const coreCheck = await checkCoreChange(romId).catch(
    (e): { changed: boolean; old_core?: string; new_core?: string; old_label?: string; new_label?: string } => {
      logError(`Watcher core-change check failed (assuming unchanged): ${e}`);
      return { changed: false };
    },
  );
  if (!coreCheck.changed) return true;
  return showCoreChangeModal(
    coreCheck.old_label ?? coreCheck.old_core ?? "Unknown",
    coreCheck.new_label ?? coreCheck.new_core ?? "Unknown",
  );
}

/** Pre-launch sync hard timeout — mirrors the Play button's `runPreLaunchSync`. */
const PRE_LAUNCH_SYNC_TIMEOUT_MS = 15000;

/**
 * Online pre-launch sync, mapped onto the gate's {@link PreLaunchSyncOutcome}.
 * The benign `savefiles_in_content_dir` skip is treated as a successful proceed
 * (no conflict, no failure) — exactly as the Play button does — so it never
 * surfaces a fallback confirm.
 *
 * Critically, this MUST NOT fail open: a throw or a hang in `preLaunchSync`
 * would otherwise propagate to the gate's blanket catch → `allow` → a silent
 * relaunch on stale saves. So the call is wrapped in a 15s timeout race AND a
 * try/catch, and on throw/timeout it returns `{ success: false, ... }` — which
 * the gate maps to `sync_failed`, surfacing the fallback confirm instead of
 * silently launching.
 */
async function preLaunchSyncWatcher(romId: number): Promise<PreLaunchSyncOutcome> {
  let result: Awaited<ReturnType<typeof preLaunchSync>>;
  try {
    result = await Promise.race([
      preLaunchSync(romId),
      new Promise<never>((_, reject) => setTimeout(() => reject(new Error("timeout")), PRE_LAUNCH_SYNC_TIMEOUT_MS)),
    ]);
  } catch (e) {
    logError(`Watcher pre-launch sync failed (surfacing fallback confirm): ${e}`);
    return { success: false, message: "Couldn't sync saves with RomM server." };
  }
  if (result.reason === SAVEFILES_IN_CONTENT_DIR_REASON) {
    return { success: true, message: result.message };
  }
  const outcome: PreLaunchSyncOutcome = { success: result.success, message: result.message };
  if (result.conflicts) outcome.conflicts = result.conflicts;
  return outcome;
}

/** Build the funnel callbacks for a given romId. */
function makeWatcherOps(romId: number): LaunchGateOps {
  return {
    migrationPending: () => getMigrationState().pending,
    ensureTrackingConfigured: async (): Promise<"proceed"> => {
      await ensureTrackingConfiguredWatcher(romId);
      return "proceed";
    },
    checkCoreChange: () => checkCoreChangeWatcher(romId),
    checkReachability: async () =>
      (
        await probeReachability().catch((e) => {
          logError(`Watcher reachability probe failed (treating as offline): ${e}`);
          return { online: false };
        })
      ).online,
    preLaunchSync: () => preLaunchSyncWatcher(romId),
    checkLocalDrift: async () =>
      (
        await checkLocalDrift(romId).catch((e) => {
          logError(`Watcher local-drift check failed (treating as not-drifted): ${e}`);
          return { drifted: false };
        })
      ).drifted,
  };
}

/** Relaunch a previously-cancelled launch. Marks the appId as skipped FIRST so
 *  this RunGame doesn't re-enter the watcher and re-gate. Used directly only for
 *  the no-romId paths (unknown appId, error fallback) where there is nothing to
 *  re-confirm; the gated path goes through {@link relaunch}. */
function bareRelaunch(appId: number): void {
  markLaunchSkipped(appId);
  const gameId = appStore.GetAppOverviewByAppID(appId)?.GetGameID?.() ?? String(appId);
  SteamClient.Apps.RunGame(gameId, "", -1, 100);
}

/** Relaunch a previously-cancelled, now-approved launch. Heals any mid-session
 *  `launch_options` drift on the shortcut first (shared bounded-race re-confirm,
 *  best-effort — a hang/null/failure still relaunches), then marks the appId as
 *  skipped immediately before this RunGame so it doesn't re-enter the watcher
 *  and re-gate. The re-confirm runs in the already-detached post-cancel portion,
 *  so it only adds a bounded (≤3s) wait to the cancel→relaunch window. */
async function relaunch(appId: number, romId: number): Promise<void> {
  await reconfirmLaunchOptions(romId, appId, "Watcher");
  bareRelaunch(appId);
}

/**
 * Act on the funnel's verdict for a cancelled launch. The launch is already
 * stopped, so each branch either relaunches (`relaunch`) or does nothing.
 *
 * Returns "retry" only from the offline-drift branch when the user asks to
 * re-probe — {@link runWatcherGate} loops on that and re-runs the gate (which
 * re-probes via the fast reachability check); every other outcome returns
 * "done".
 */
async function handleWatcherVerdict(verdict: GateVerdict, appId: number, romId: number): Promise<"done" | "retry"> {
  switch (verdict.decision) {
    case "allow":
      await relaunch(appId, romId);
      return "done";
    case "abort":
      // The user saw setup/core UI and declined — already cancelled, nothing to do.
      return "done";
    case "block":
      if (verdict.reason === "migration_pending") {
        toaster.toast({ title: "RomM Sync", body: MIGRATION_TOAST_BODY });
      }
      return "done";
    case "conflict": {
      const resolution = await handleConflicts(verdict.conflicts);
      if (resolution === "cancel") return "done";
      // Conflicts resolved — notify sibling components to refresh, then relaunch.
      globalThis.dispatchEvent(new CustomEvent("romm_data_changed", { detail: { type: "save_sync", rom_id: romId } }));
      await relaunch(appId, romId);
      return "done";
    }
    case "offline_drift": {
      const choice = await showOfflineDriftModal();
      if (choice === "start_anyway") await relaunch(appId, romId);
      if (choice === "retry") return "retry";
      return "done";
    }
    case "sync_failed": {
      const proceed = await showFallbackLaunchModal(verdict.message);
      if (proceed) await relaunch(appId, romId);
      return "done";
    }
  }
}

/**
 * Run the shared launch gate and act on its verdict, looping while the user
 * keeps choosing "Retry" on the offline-drift modal. Each retry re-runs
 * {@link runLaunchGate} (re-probing connectivity via the fast reachability
 * check) and acts on the NEW verdict — online now relaunches via the normal
 * path; still offline + drift re-shows the offline modal. A gate throw fails
 * open to `allow` so a gate bug never traps the user's already-cancelled launch.
 */
async function runWatcherGate(appId: number, romId: number): Promise<void> {
  let verdict = await runLaunchGate(appId, romId, makeWatcherOps(romId)).catch((e): GateVerdict => {
    logError(`Watcher gate threw (failing open to allow): ${e}`);
    return { decision: "allow" };
  });
  while ((await handleWatcherVerdict(verdict, appId, romId)) === "retry") {
    verdict = await runLaunchGate(appId, romId, makeWatcherOps(romId)).catch((e): GateVerdict => {
      logError(`Watcher gate threw (failing open to allow): ${e}`);
      return { decision: "allow" };
    });
  }
}

/**
 * Is this ROM installed on disk? The funnel assumes an installed ROM; an
 * uninstalled one is a hard block (the ROM is gone — no "Start Anyway").
 * `get_installed_rom` is the live truth: a returned record means installed, a
 * `null` means not installed. Only when the call THROWS (transport hiccup) do we
 * fall back to the cached detail's `installed` flag, so a transient error never
 * trap-blocks a genuinely-installed ROM.
 */
async function isRomInstalled(appId: number, romId: number): Promise<boolean> {
  try {
    return (await getInstalledRom(romId)) != null;
  } catch (e) {
    logError(`Watcher installed check threw (falling back to cached install flag): ${e}`);
    const cached = await getCachedGameDetail(appId).catch((cacheErr) => {
      logError(`Watcher cached-detail fallback failed (treating as not installed): ${cacheErr}`);
      return null;
    });
    return cached?.installed === true;
  }
}

export function registerLaunchInterceptor(): void {
  gameActionHook = SteamClient.Apps.RegisterForGameActionStart(
    (gameActionId: number, appIdStr: string, action: string, _launchSource: number) => {
      if (action !== "LaunchApp") return;

      const appId = Number.parseInt(appIdStr, 10);
      if (Number.isNaN(appId) || !isRomMAppId(appId)) return;

      // One-shot skip: a gated relaunch (the watcher's own RunGame) or a
      // Play-button launch already ran the funnel — do NOT re-gate it.
      if (consumeLaunchSkip(appId)) return;

      // CANCEL FIRST — synchronously, before any await. This wins the race
      // against the un-pausable launch: from here the launch is stopped and we
      // relaunch only on approval.
      SteamClient.Apps.CancelGameAction(gameActionId);

      detach(
        (async () => {
          try {
            // Fire-and-forget migration refresh — picks up RetroArch sort
            // changes made via the in-game Quick Menu before the prior session.
            refreshMigrationState()
              .then(({ retrodeck, save_sort }) => {
                setMigrationStatus(retrodeck);
                setSaveSortMigrationStatus(save_sort);
              })
              .catch((e) => logError(`Pre-launch migration refresh failed: ${e}`));

            // Resolve romId synchronously from the session map snapshot. An
            // unknown appId is not ours to gate — relaunch and bail.
            const romId = getAppIdRomIdMapSnapshot()[String(appId)];
            if (romId == null) {
              bareRelaunch(appId);
              return;
            }

            // The funnel assumes an installed ROM. Not installed → hard block
            // (no relaunch): the ROM is gone.
            if (!(await isRomInstalled(appId, romId))) {
              toaster.toast({
                title: "RomM Sync",
                body: "ROM not downloaded. Open the plugin to download it first.",
              });
              return;
            }

            await runWatcherGate(appId, romId);
          } catch (e) {
            // Any unexpected error must NEVER trap the user's game — relaunch.
            // Use the bare relaunch (no re-confirm): the failure may be the
            // re-confirm's own dependency, and the priority here is escaping the
            // cancelled state, not healing drift.
            logError(`Launch interceptor error: ${e}`);
            bareRelaunch(appId);
          }
        })(),
      );
    },
  );

  logInfo("Launch interceptor registered");
}

export function unregisterLaunchInterceptor(): void {
  if (gameActionHook) {
    gameActionHook.unregister();
    gameActionHook = null;
  }
  logInfo("Launch interceptor unregistered");
}
