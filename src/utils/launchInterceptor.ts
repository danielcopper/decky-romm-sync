/**
 * Global launch interceptor — cancels Steam game launches for RomM shortcuts
 * when the ROM is not downloaded or a save conflict needs resolution.
 *
 * Registered on plugin load, unregistered on unload.
 */

import { toaster } from "@decky/api";
import { isRomMAppId } from "../patches/gameDetailPatch";
import { getInstalledRom, getSaveStatus, getSaveSyncSettings, refreshMigrationState, logInfo, logError } from "../api/backend";
import { setMigrationStatus } from "./migrationStore";
import { setSaveSortMigrationStatus } from "./saveSortMigrationStore";
import { hasAnySaveConflict } from "./saveStatus";

let gameActionHook: { unregister: () => void } | null = null;

export function registerLaunchInterceptor(): void {
  gameActionHook = SteamClient.Apps.RegisterForGameActionStart(
    async (gameActionId: number, appIdStr: string, action: string, _launchSource: number) => {
      if (action !== "LaunchApp") return;

      const appId = parseInt(appIdStr, 10);
      if (isNaN(appId) || !isRomMAppId(appId)) return;

      // Fire-and-forget migration refresh — picks up RetroArch sort setting
      // changes made via the in-game Quick Menu before the previous session.
      // Must not block the launch: the user pressing Play means "launch now".
      refreshMigrationState()
        .then(({ retrodeck, save_sort }) => {
          setMigrationStatus(retrodeck);
          setSaveSortMigrationStatus(save_sort);
        })
        .catch((e) => logError(`Pre-launch migration refresh failed: ${e}`));

      // Check if ROM is installed
      try {
        // We need the rom_id — look it up from registry
        // The getRomBySteamAppId call is heavier but necessary for accurate check
        const { getRomBySteamAppId } = await import("../api/backend");
        const rom = await getRomBySteamAppId(appId);
        if (!rom) return; // Not a RomM game, let it pass

        const installed = await getInstalledRom(rom.rom_id);
        if (!installed) {
          SteamClient.Apps.CancelGameAction(gameActionId);
          toaster.toast({
            title: "RomM Sync",
            body: "ROM not downloaded. Open the game page to download it first.",
          });
          return;
        }

        // Check for save conflicts in ask_me mode
        try {
          const settings = await getSaveSyncSettings();
          if (settings.conflict_mode === "ask_me") {
            const saveStatus = await getSaveStatus(rom.rom_id);
            const hasConflict = hasAnySaveConflict(saveStatus);
            if (hasConflict) {
              SteamClient.Apps.CancelGameAction(gameActionId);
              toaster.toast({
                title: "RomM Save Sync",
                body: "Save conflict detected \u2014 open game page to resolve before playing",
              });
              return;
            }
          }
        } catch {
          // Non-critical — let the game launch if we can't check conflicts
        }
      } catch (e) {
        logError(`Launch interceptor error: ${e}`);
        // On error, don't block the launch
      }
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
