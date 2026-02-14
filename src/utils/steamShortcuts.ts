import type { SyncAddItem } from "../types";

const ROMM_MARKER = "romm:";

const delay = (ms: number) => new Promise<void>((r) => setTimeout(r, ms));

/**
 * Scan all non-Steam shortcuts and return those managed by RomM.
 * Returns Map<romId, steamAppId>.
 */
export async function getExistingRomMShortcuts(): Promise<Map<number, number>> {
  const result = new Map<number, number>();

  if (typeof collectionStore === "undefined") return result;

  const appIds = Array.from(collectionStore.deckDesktopApps.apps.keys());

  for (const appId of appIds) {
    const launchOptions = await getLaunchOptions(appId);
    if (launchOptions && launchOptions.includes(ROMM_MARKER)) {
      const match = launchOptions.match(/romm:(\d+)/);
      if (match) {
        result.set(Number(match[1]), appId);
      }
    }
  }

  return result;
}

function getLaunchOptions(appId: number): Promise<string | null> {
  return new Promise((resolve) => {
    let resolved = false;
    const reg = SteamClient.Apps.RegisterForAppDetails(appId, (details: any) => {
      if (!resolved) {
        resolved = true;
        reg.unregister();
        resolve(details?.strLaunchOptions ?? details?.LaunchOptions ?? null);
      }
    });
    // Timeout after 2s to avoid hanging
    setTimeout(() => {
      if (!resolved) {
        resolved = true;
        reg.unregister();
        resolve(null);
      }
    }, 2000);
  });
}

/**
 * Add a single Steam shortcut. Returns the new steam app_id, or null on failure.
 * Waits 300ms after AddShortcut for Steam to register the app before setting properties.
 * Artwork is handled via file-based grid by the backend (grid/{id}p.png).
 */
export async function addShortcut(data: SyncAddItem): Promise<number | null> {
  try {
    const appId = await SteamClient.Apps.AddShortcut(
      data.name,
      data.exe,
      "",
      "",
    );

    if (!appId) return null;

    // Wait for Steam to register the new app before setting properties
    await delay(500);

    SteamClient.Apps.SetShortcutName(appId, data.name);
    SteamClient.Apps.SetShortcutExe(appId, data.exe);
    SteamClient.Apps.SetShortcutStartDir(appId, data.start_dir);
    SteamClient.Apps.SetAppLaunchOptions(appId, data.launch_options);

    return appId;
  } catch (e) {
    console.error(`[RomM] Failed to add shortcut for ${data.name}:`, e);
    return null;
  }
}

/**
 * Remove a single Steam shortcut by app_id.
 */
export function removeShortcut(appId: number): void {
  try {
    SteamClient.Apps.RemoveShortcut(appId);
  } catch (e) {
    console.error(`[RomM] Failed to remove shortcut ${appId}:`, e);
  }
}
