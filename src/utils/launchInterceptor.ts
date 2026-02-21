/**
 * Global launch interceptor — cancels Steam game launches for RomM shortcuts
 * when the ROM is not downloaded or a save conflict needs resolution.
 *
 * Registered on plugin load, unregistered on unload.
 */

import { toaster } from "@decky/api";
import { isRomMAppId } from "../patches/gameDetailPatch";
import { getInstalledRom, getPendingConflicts, getSaveSyncSettings } from "../api/backend";

let gameActionHook: { unregister: () => void } | null = null;

export function registerLaunchInterceptor(): void {
  gameActionHook = SteamClient.Apps.RegisterForGameActionStart(
    async (gameActionId: number, appIdStr: string, action: string, _launchSource: number) => {
      if (action !== "LaunchApp") return;

      const appId = parseInt(appIdStr, 10);
      if (isNaN(appId) || !isRomMAppId(appId)) return;

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
            const conflictsResult = await getPendingConflicts();
            const romConflicts = (conflictsResult.conflicts || []).filter(
              (c) => c.rom_id === rom.rom_id,
            );
            if (romConflicts.length > 0) {
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
        console.error("[RomM] Launch interceptor error:", e);
        // On error, don't block the launch
      }
    },
  );

  console.log("[RomM] Launch interceptor registered");
}

export function unregisterLaunchInterceptor(): void {
  if (gameActionHook) {
    gameActionHook.unregister();
    gameActionHook = null;
  }
  console.log("[RomM] Launch interceptor unregistered");
}
