import type { SyncAddItem } from "../types";
import { getAppIdRomIdMap, syncHeartbeat, logError } from "../api/backend";

/**
 * Ownership marker: RomM-managed shortcuts launch through the plugin's
 * `bin/rom-launcher` exec wrapper. A shortcut whose `strShortcutExe` ends with
 * this suffix is ours regardless of its launch options (which now carry the
 * full RetroDECK command, not a `romm:<id>` marker).
 */
const ROM_LAUNCHER_SUFFIX = "/bin/rom-launcher";

const HEARTBEAT_INTERVAL_MS = 10_000;

const delay = (ms: number) => new Promise<void>((r) => setTimeout(r, ms));

/**
 * Resolve a shortcut's `SteamAppDetails` via the one-shot RegisterForAppDetails
 * pattern. Resolves with the first details object the runtime delivers, or
 * ``null`` if none arrives within ``timeoutMs`` (the runtime can fire with no
 * details before the app's data loads — those early ``undefined`` fires are
 * ignored).
 */
function getAppDetails(appId: number, timeoutMs = 2000): Promise<SteamAppDetails | null> {
  return new Promise((resolve) => {
    let resolved = false;
    // Declared with `let` BEFORE RegisterForAppDetails so a (hypothetical)
    // synchronous callback fire can't hit the temporal dead zone when finish()
    // reads reg.
    // eslint-disable-next-line prefer-const -- the `let`-before-register ordering is the TDZ guard; `prefer-const` only sees the single assignment and can't model the closure reading `reg` before the assignment line executes.
    let reg: { unregister: () => void } | undefined;
    const finish = (value: SteamAppDetails | null) => {
      if (resolved) return;
      resolved = true;
      reg?.unregister();
      resolve(value);
    };
    reg = SteamClient.Apps.RegisterForAppDetails(appId, (details) => {
      if (details) finish(details);
    });
    setTimeout(() => finish(null), timeoutMs);
  });
}

/**
 * Set an existing shortcut's launch options and confirm the write landed.
 *
 * Every Steam ``Set*`` returns ``void`` with no success signal, so we fire
 * ``SetAppLaunchOptions`` then poll ``RegisterForAppDetails`` until the
 * read-back ``strLaunchOptions`` matches ``value``. Resolves ``true`` on a
 * confirmed match, ``false`` if no matching read-back arrives within
 * ``timeoutMs``. Setting ``""`` (the uninstalled-placeholder value) is valid
 * and confirms against an empty read-back.
 */
export function setLaunchOptionsConfirmed(appId: number, value: string, timeoutMs = 2000): Promise<boolean> {
  return new Promise((resolve) => {
    let resolved = false;
    // Declared with `let` BEFORE RegisterForAppDetails so a (hypothetical)
    // synchronous callback fire can't hit the temporal dead zone when finish()
    // reads reg.
    // eslint-disable-next-line prefer-const -- the `let`-before-register ordering is the TDZ guard; `prefer-const` only sees the single assignment and can't model the closure reading `reg` before the assignment line executes.
    let reg: { unregister: () => void } | undefined;
    const finish = (matched: boolean) => {
      if (resolved) return;
      resolved = true;
      reg?.unregister();
      resolve(matched);
    };

    SteamClient.Apps.SetAppLaunchOptions(appId, value);

    reg = SteamClient.Apps.RegisterForAppDetails(appId, (details) => {
      if (!details) return;
      const current = details.strLaunchOptions ?? details.LaunchOptions ?? "";
      if (current === value) finish(true);
    });

    setTimeout(() => finish(false), timeoutMs);
  });
}

/**
 * Scan all non-Steam shortcuts and return those managed by RomM.
 *
 * A shortcut is RomM-owned when BOTH hold: its `strShortcutExe` ends with
 * `/bin/rom-launcher` (live-in-Steam ownership marker) AND its appId is bound
 * to a rom_id in the backend's `get_app_id_rom_id_map()` (the authoritative
 * rom_id↔appId binding now that launch options no longer carry the id). After
 * a DB reset the backend map is empty, so our shortcuts are detected by exe but
 * remain unmapped — they're treated as orphans and re-sync recreates them.
 *
 * Returns Map<romId, steamAppId>.
 */
export async function getExistingRomMShortcuts(): Promise<Map<number, number>> {
  const result = new Map<number, number>();

  if (typeof collectionStore === "undefined") return result;

  const deckApps = collectionStore.deckDesktopApps?.apps;
  if (!deckApps) return result;

  const appIds = Array.from(deckApps.keys());

  // Detect our shortcuts by exe, in parallel batches to avoid 2s-per-shortcut
  // serial overhead from RegisterForAppDetails. A large library makes this scan
  // take tens of seconds, so emit a heartbeat every 10s between batches —
  // otherwise the backend's per-unit heartbeat timeout cancels the run before
  // the scan finishes.
  const ourAppIds: number[] = [];
  const CONCURRENCY = 10;
  let lastHeartbeat = Date.now();
  for (let i = 0; i < appIds.length; i += CONCURRENCY) {
    const batch = appIds.slice(i, i + CONCURRENCY);
    const entries = await Promise.all(
      batch.map((appId) => getAppDetails(appId).then((details) => ({ appId, exe: details?.strShortcutExe ?? "" }))),
    );
    for (const { appId, exe } of entries) {
      if (exe.endsWith(ROM_LAUNCHER_SUFFIX)) ourAppIds.push(appId);
    }
    if (Date.now() - lastHeartbeat > HEARTBEAT_INTERVAL_MS) {
      syncHeartbeat().catch(() => {});
      lastHeartbeat = Date.now();
    }
  }

  if (ourAppIds.length === 0) return result;

  // Resolve rom_id for each of our appIds via the authoritative backend map.
  // The map is keyed by appId-string → rom_id; keep only the intersection of
  // (our exe) AND (bound in the backend).
  let appIdToRomId: Record<string, number>;
  try {
    appIdToRomId = await getAppIdRomIdMap();
  } catch (e) {
    logError(`getExistingRomMShortcuts: failed to load app-id↔rom-id map: ${e}`);
    return result;
  }

  for (const appId of ourAppIds) {
    const romId = appIdToRomId[String(appId)];
    if (typeof romId === "number") result.set(romId, appId);
  }

  return result;
}

/**
 * Add a single Steam shortcut. Returns the new steam app_id, or null on failure.
 */
export async function addShortcut(data: SyncAddItem): Promise<number | null> {
  try {
    // AddShortcut ignores most params (confirmed by MoonDeck plugin) —
    // must use Set* calls after creation to apply name, exe, startDir, launchOptions.
    const appId = await SteamClient.Apps.AddShortcut(data.name, data.exe, "", "");

    if (!appId) return null;

    // Wait for Steam to register the new app before setting properties
    await delay(500);

    SteamClient.Apps.SetShortcutName(appId, data.name);
    SteamClient.Apps.SetShortcutExe(appId, data.exe);
    SteamClient.Apps.SetShortcutStartDir(appId, data.start_dir);
    // launch_options may be "" for an uninstalled ROM (placeholder) — setting
    // "" is fine; the confirm-poll matches against the empty read-back.
    await setLaunchOptionsConfirmed(appId, data.launch_options);

    return appId;
  } catch (e) {
    logError(`Failed to add shortcut for ${data.name}: ${e}`);
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
    logError(`Failed to remove shortcut ${appId}: ${e}`);
  }
}
