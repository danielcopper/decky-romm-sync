/**
 * Global launch interceptor — cancels Steam game launches for RomM shortcuts
 * when the ROM is not downloaded or a save conflict needs resolution.
 *
 * Registered on plugin load, unregistered on unload.
 */

import { toaster } from "@decky/api";
import { isRomMAppId } from "../patches/gameDetailPatch";
import { evaluateLaunch, refreshMigrationState, logInfo, logError } from "../api/backend";
import { getMigrationState, setMigrationStatus } from "./migrationStore";
import { setSaveSortMigrationStatus } from "./saveSortMigrationStore";

let gameActionHook: { unregister: () => void } | null = null;

export function registerLaunchInterceptor(): void {
  gameActionHook = SteamClient.Apps.RegisterForGameActionStart(
    async (gameActionId: number, appIdStr: string, action: string, _launchSource: number) => {
      if (action !== "LaunchApp") return;

      const appId = parseInt(appIdStr, 10);
      if (isNaN(appId) || !isRomMAppId(appId)) return;

      // Block launch if a RetroDECK migration is pending. Backend also blocks
      // via @migration_blocked, but cancelling the Steam action here prevents
      // Steam from even trying to start the game. Synchronous in-memory check
      // so it stays on the frontend.
      if (getMigrationState().pending) {
        SteamClient.Apps.CancelGameAction(gameActionId);
        toaster.toast({
          title: "RomM Sync",
          body: "Pending RetroDECK migration. Open the plugin QAM to migrate or dismiss.",
        });
        return;
      }

      // Fire-and-forget migration refresh — picks up RetroArch sort setting
      // changes made via the in-game Quick Menu before the previous session.
      // Must not block the launch: the user pressing Play means "launch now".
      // Exception: pending RetroDECK migration is handled above as an explicit
      // block, because the alternative is silent save-data loss.
      refreshMigrationState()
        .then(({ retrodeck, save_sort }) => {
          setMigrationStatus(retrodeck);
          setSaveSortMigrationStatus(save_sort);
        })
        .catch((e) => logError(`Pre-launch migration refresh failed: ${e}`));

      // Single round-trip — backend composes the rom-lookup + installed-check
      // + save-status read and returns a typed verdict.
      try {
        const verdict = await evaluateLaunch(appId);
        if (verdict.action === "block") {
          SteamClient.Apps.CancelGameAction(gameActionId);
          if (verdict.toast_title && verdict.toast_body) {
            toaster.toast({ title: verdict.toast_title, body: verdict.toast_body });
          }
        }
      } catch (e) {
        logError(`Launch interceptor error: ${e}`);
        // On error, don't block the launch.
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
