import type { SyncAddItem } from "../types";
import { getAppIdRomIdMap, syncHeartbeat, logError, logInfo } from "../api/backend";
import { delay } from "./pacedOps";

/**
 * Ownership marker: RomM-managed shortcuts launch through the plugin's
 * `bin/rom-launcher` exec wrapper. A shortcut whose `strShortcutExe` ends with
 * this suffix is ours regardless of its launch options (which now carry the
 * full RetroDECK command, not a `romm:<id>` marker).
 */
const ROM_LAUNCHER_SUFFIX = "/bin/rom-launcher";

export function isRomMShortcutDetails(details: SteamAppDetails | null): details is SteamAppDetails {
  return (
    typeof details?.strShortcutExe === "string" &&
    details.strShortcutExe.replace(/^"|"$/g, "").endsWith(ROM_LAUNCHER_SUFFIX)
  );
}

const HEARTBEAT_INTERVAL_MS = 10_000;

/**
 * Resolve a shortcut's `SteamAppDetails` via the one-shot RegisterForAppDetails
 * pattern. Resolves with the first details object the runtime delivers, or
 * ``null`` if none arrives within ``timeoutMs`` (the runtime can fire with no
 * details before the app's data loads — those early ``undefined`` fires are
 * ignored).
 */
export function getAppDetails(appId: number, timeoutMs = 2000): Promise<SteamAppDetails | null> {
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
 * Scan Steam's live shortcut store and return the appIds of every RomM-owned
 * shortcut — those whose `strShortcutExe` ends with `/bin/rom-launcher` (the
 * live-in-Steam ownership marker), regardless of any backend binding.
 *
 * Returns the raw live appId list, or `null` when the scan could **not** run
 * because Steam's shortcut store was unreadable (`collectionStore` /
 * `deckDesktopApps.apps` absent). The `null`-vs-`[]` distinction is
 * load-bearing for reconcile: `[]` means "scan ran, found zero RomM shortcuts"
 * (a real signal — unbind everything), whereas `null` means "could not look"
 * (callers must NOT reconcile against it, or they'd unbind every binding on a
 * transiently-broken store).
 *
 * Detection runs in parallel batches (RegisterForAppDetails is ~2s serial per
 * shortcut); a heartbeat every 10s keeps the backend's per-unit timeout from
 * cancelling a long scan over a large library.
 */
export async function getLiveRomMShortcutAppIds(): Promise<number[] | null> {
  if (typeof collectionStore === "undefined") return null;

  const deckApps = collectionStore.deckDesktopApps?.apps;
  if (!deckApps) return null;

  const appIds = Array.from(deckApps.keys());

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

  return ourAppIds;
}

/**
 * Enumerate the appIds of ALL live non-Steam shortcuts — RomM-owned AND
 * foreign — from Steam's collection store. This is the keep-set for the
 * orphaned grid-image cleanup, so unlike `getLiveRomMShortcutAppIds` there is
 * deliberately NO rom-launcher filter: every live shortcut's artwork must be
 * protected, not just ours.
 *
 * Returns `null` when the scan could not run (`collectionStore` /
 * `deckDesktopApps.apps` unreadable). The `null`-vs-`[]` distinction is
 * load-bearing: `[]` means "scan ran, zero shortcuts exist" (a real keep-set),
 * `null` means "could not look" — callers must abort and delete nothing.
 */
export function getAllNonSteamShortcutAppIds(): number[] | null {
  if (typeof collectionStore === "undefined") return null;

  const deckApps = collectionStore.deckDesktopApps?.apps;
  if (!deckApps) return null;

  return Array.from(deckApps.keys());
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
 * *preScanned* lets a caller that already ran the once-per-run
 * `getLiveRomMShortcutAppIds()` sweep hand its result in, so the expensive
 * per-shortcut `RegisterForAppDetails` scan runs at most once per sync run
 * (#1366). Omit it to scan internally; a `null` *preScanned* (store unreadable)
 * yields an empty map, exactly like a `null` internal scan.
 *
 * Returns Map<romId, steamAppId>.
 */
export async function getExistingRomMShortcuts(preScanned?: number[] | null): Promise<Map<number, number>> {
  const result = new Map<number, number>();

  const ourAppIds = preScanned !== undefined ? preScanned : await getLiveRomMShortcutAppIds();
  if (!ourAppIds || ourAppIds.length === 0) return result;

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
 * Poll ``appStore`` until the new shortcut's overview is registered, or
 * ``timeoutMs`` elapses. A freshly created shortcut's overview appears
 * asynchronously; polling for readiness (MoonDeck pattern) replaces a fixed
 * worst-case wait — the common case proceeds in ~100ms instead of a blind
 * 500ms, and the timeout ceiling keeps the net behaviour of the old wait when
 * the overview is slow to appear. Resolves ``true`` once the overview exists,
 * ``false`` on timeout (the caller proceeds regardless).
 */
async function waitForAppOverview(appId: number, timeoutMs: number): Promise<boolean> {
  const POLL_INTERVAL_MS = 100;
  const deadline = Date.now() + timeoutMs;
  for (;;) {
    if (appStore.GetAppOverviewByAppID(appId)) return true;
    if (Date.now() >= deadline) return false;
    await delay(POLL_INTERVAL_MS);
  }
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

    // Wait for Steam to register the new app's overview before setting
    // properties. Poll for readiness instead of a blind 500ms; on timeout,
    // proceed anyway (same net behaviour as the old fixed wait).
    if (!(await waitForAppOverview(appId, 1000))) {
      logInfo(`addShortcut: overview for ${appId} not ready within 1000ms; proceeding anyway`);
    }

    SteamClient.Apps.SetShortcutName(appId, data.name);
    SteamClient.Apps.SetShortcutExe(appId, data.exe);
    SteamClient.Apps.SetShortcutStartDir(appId, data.start_dir);
    // A freshly created shortcut's launch options are already empty. For an
    // uninstalled ROM (launch_options ""), there is nothing to write or confirm,
    // so skip both SetAppLaunchOptions and the confirm poll — the confirm poll's
    // RegisterForAppDetails forces Steam to load+cache a fat AppDetails object
    // per call, so skipping it for the majority uninstalled case avoids that
    // heap hit. A non-empty command (installed ROM) still takes the confirmed
    // write (#827).
    if (data.launch_options !== "") {
      await setLaunchOptionsConfirmed(appId, data.launch_options);
    }

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

/** Steam's live non-Steam shortcut map, or `null` when it can't be read. */
function readDesktopAppStore(): Map<number, unknown> | null {
  if (typeof collectionStore === "undefined") return null;
  return collectionStore.deckDesktopApps?.apps ?? null;
}

export interface ShortcutRemovalOutcome {
  status: "confirmed" | "not_attempted" | "attempted_unconfirmed";
}

/** Distinguish a pre-mutation refusal from an unconfirmed mutation attempt. */
export async function removeShortcutConfirmedOutcome(
  appId: number,
  timeoutMs = 3000,
  ownershipAlreadyChecked = false,
): Promise<ShortcutRemovalOutcome> {
  const store = readDesktopAppStore();
  if (!store?.has(appId)) return { status: "not_attempted" };
  if (!ownershipAlreadyChecked && !isRomMShortcutDetails(await getAppDetails(appId))) {
    return { status: "not_attempted" };
  }
  try {
    SteamClient.Apps.RemoveShortcut(appId);
  } catch (e) {
    logError(`Failed to remove shortcut ${appId}: ${e}`);
    return { status: "not_attempted" };
  }
  const deadline = Date.now() + timeoutMs;
  for (;;) {
    const apps = readDesktopAppStore();
    if (!apps) return { status: "attempted_unconfirmed" };
    if (!apps.has(appId)) return { status: "confirmed" };
    if (Date.now() >= deadline) return { status: "attempted_unconfirmed" };
    await delay(100);
  }
}
