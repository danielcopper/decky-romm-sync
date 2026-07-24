import { fetchCoverBase64, logError, reportPruneAction, switchVersion } from "../api/backend";
import type { PruneSteamSnapshot, ReportPruneActionRequest } from "../api/backend";
import { getAppDetails, removeShortcutConfirmed, setLaunchOptionsConfirmed } from "./steamShortcuts";

export type PruneActionRequired =
  | { run_id: string; action_token: string; action: "capture_shortcut_snapshot"; app_id: number }
  | {
      run_id: string;
      action_token: string;
      action: "repoint_shortcut";
      app_id: number;
      target_rom_id: number;
      allow_stranded: boolean;
    }
  | { run_id: string; action_token: string; action: "remove_shortcut"; app_id: number };

const SNAPSHOT_STRING_CHARS = 1024;
const SNAPSHOT_COLLECTION_CHARS = 512;
const SNAPSHOT_BUDGET_BYTES = 56 * 1024;

function bounded(value: string | undefined, limit = SNAPSHOT_STRING_CHARS): string {
  return (value ?? "").slice(0, limit);
}

function asciiJsonSize(value: unknown): number {
  let bytes = 0;
  for (const char of JSON.stringify(value)) {
    const codePoint = char.codePointAt(0) ?? 0;
    bytes += codePoint <= 0x7f ? 1 : codePoint <= 0xffff ? 6 : 12;
  }
  return bytes;
}

async function captureShortcutSnapshot(appId: number): Promise<PruneSteamSnapshot | null> {
  if (typeof collectionStore === "undefined") return null;
  const details = await getAppDetails(appId);
  if (!details) return null;
  const overview = typeof appStore === "undefined" ? null : appStore.GetAppOverviewByAppID(appId);
  const collections = collectionStore.userCollections
    .filter((collection) => collection.apps.has(appId))
    .slice(0, 256)
    .map((collection) => ({
      id: bounded(collection.id, SNAPSHOT_COLLECTION_CHARS),
      name: bounded(collection.displayName, SNAPSHOT_COLLECTION_CHARS),
    }));
  const snapshot: PruneSteamSnapshot = {
    app_id: appId,
    name: bounded(details.strDisplayName ?? overview?.strDisplayName),
    exe: bounded(details.strShortcutExe),
    start_dir: bounded(details.strShortcutStartDir),
    launch_options: bounded(details.strLaunchOptions ?? details.LaunchOptions),
    minutes_playtime_forever: overview?.minutes_playtime_forever ?? null,
    minutes_playtime_last_two_weeks: overview?.minutes_playtime_last_two_weeks ?? null,
    last_played: overview?.rt_last_time_played ?? null,
    collections,
  };
  while (snapshot.collections.length > 0 && asciiJsonSize(snapshot) > SNAPSHOT_BUDGET_BYTES) {
    snapshot.collections.pop();
  }
  return snapshot;
}

async function applyRepoint(action: Extract<PruneActionRequired, { action: "repoint_shortcut" }>): Promise<string> {
  const result = await switchVersion(action.app_id, action.target_rom_id, action.allow_stranded);
  if (!result.success || result.rom_id !== action.target_rom_id || result.app_id !== action.app_id) {
    throw new Error(result.success ? "Version switch returned the wrong target" : result.message);
  }
  if (!(await setLaunchOptionsConfirmed(result.app_id, result.launch_options))) {
    throw new Error("Steam did not confirm the target launch command");
  }
  const cover = await fetchCoverBase64(result.rom_id);
  if (cover.base64) {
    await SteamClient.Apps.SetCustomArtworkForApp(result.app_id, cover.base64, "png", 0);
  }
  return "Shortcut repointed and launch command confirmed.";
}

export async function handlePruneAction(action: PruneActionRequired): Promise<void> {
  let report: ReportPruneActionRequest;
  try {
    if (action.action === "capture_shortcut_snapshot") {
      const snapshot = await captureShortcutSnapshot(action.app_id);
      if (!snapshot) throw new Error("Steam shortcut details or collection state were unavailable");
      report = {
        run_id: action.run_id,
        action_token: action.action_token,
        success: true,
        message: "Steam shortcut state captured.",
        snapshot,
      };
    } else if (action.action === "repoint_shortcut") {
      report = {
        run_id: action.run_id,
        action_token: action.action_token,
        success: true,
        message: await applyRepoint(action),
      };
    } else {
      if (!(await removeShortcutConfirmed(action.app_id))) {
        throw new Error("Steam did not confirm shortcut removal");
      }
      report = {
        run_id: action.run_id,
        action_token: action.action_token,
        success: true,
        message: "Steam confirmed shortcut removal.",
      };
    }
  } catch (e) {
    report = {
      run_id: action.run_id,
      action_token: action.action_token,
      success: false,
      reason: "steam_action_failed",
      message: e instanceof Error ? e.message : String(e),
    };
  }
  try {
    await reportPruneAction(report);
  } catch (e) {
    logError(`Failed to report prune action ${action.action_token}: ${e}`);
  }
}
