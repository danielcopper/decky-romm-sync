import { logError, reportPruneAction } from "../api/backend";
import type {
  CompletePruneActionRequest,
  PruneSteamSnapshot,
  ReportPruneActionRequest,
  SwitchVersionSuccess,
} from "../api/backend";
import { getAppDetails, isRomMShortcutDetails, removeShortcutConfirmed } from "./steamShortcuts";
import { applyCommittedVersionSwitch } from "./versionSwitchApplication";

export type PruneActionRequired =
  | { run_id: string; action_token: string; action: "capture_shortcut_snapshot"; app_id: number }
  | {
      run_id: string;
      action_token: string;
      action: "repoint_shortcut";
      app_id: number;
      target_rom_id: number;
      launch_options: string;
      target_installed: boolean;
    }
  | { run_id: string; action_token: string; action: "remove_shortcut"; app_id: number };

const SNAPSHOT_BUDGET_BYTES = 56 * 1024;
const REPORT_ATTEMPTS = 3;
const handledTokens = new Set<string>();
let actionQueue: Promise<void> = Promise.resolve();
let actionGeneration = 0;

function asciiJsonSize(value: unknown): number {
  let bytes = 0;
  for (const char of JSON.stringify(value)) {
    const codePoint = char.codePointAt(0) ?? 0;
    bytes += codePoint <= 0x7f ? 1 : codePoint <= 0xffff ? 6 : 12;
  }
  return bytes;
}

function isPruneAction(value: unknown): value is PruneActionRequired {
  if (!value || typeof value !== "object") return false;
  const item = value as Record<string, unknown>;
  if (typeof item.run_id !== "string" || typeof item.action_token !== "string" || typeof item.app_id !== "number") {
    return false;
  }
  if (item.action === "capture_shortcut_snapshot" || item.action === "remove_shortcut") return true;
  return (
    item.action === "repoint_shortcut" &&
    typeof item.target_rom_id === "number" &&
    typeof item.launch_options === "string" &&
    typeof item.target_installed === "boolean"
  );
}

function requireString(value: unknown, field: string): string {
  if (typeof value !== "string") throw new Error(`Steam shortcut ${field} was unavailable`);
  return value;
}

async function captureShortcutSnapshot(appId: number): Promise<PruneSteamSnapshot> {
  if (typeof collectionStore === "undefined" || typeof appStore === "undefined") {
    throw new Error("Steam shortcut, collection, or playtime state was unavailable");
  }
  const details = await getAppDetails(appId);
  if (!isRomMShortcutDetails(details)) throw new Error("The live shortcut is not owned by RomM Sync");
  const overview = appStore.GetAppOverviewByAppID(appId);
  if (!details || !overview) throw new Error("Steam shortcut playtime state was unavailable");
  const collections = collectionStore.userCollections
    .filter((collection) => collection.apps.has(appId))
    .map((collection) => ({
      id: requireString(collection.id, "collection id"),
      name: requireString(collection.displayName, "collection name"),
    }));
  const snapshot: PruneSteamSnapshot = {
    app_id: appId,
    name: requireString(details.strDisplayName ?? overview.strDisplayName, "name"),
    exe: requireString(details.strShortcutExe, "executable"),
    start_dir: requireString(details.strShortcutStartDir, "start directory"),
    launch_options: requireString(details.strLaunchOptions ?? details.LaunchOptions, "launch options"),
    minutes_playtime_forever: overview.minutes_playtime_forever ?? null,
    minutes_playtime_last_two_weeks: overview.minutes_playtime_last_two_weeks ?? null,
    last_played: overview.rt_last_time_played ?? null,
    collections,
  };
  if (asciiJsonSize(snapshot) > SNAPSHOT_BUDGET_BYTES) {
    throw new Error("Complete Steam shortcut state is too large for a safe recovery snapshot");
  }
  return snapshot;
}

async function reportWithRetry(request: ReportPruneActionRequest): Promise<boolean> {
  let lastMessage = "Action report was rejected.";
  for (let attempt = 1; attempt <= REPORT_ATTEMPTS; attempt++) {
    try {
      const result = await reportPruneAction(request);
      if (result.success) return true;
      lastMessage = result.message;
      if (
        result.reason === "stale_action" ||
        result.reason === "local_state_changed" ||
        result.reason === "action_already_claimed"
      )
        break;
    } catch (e) {
      lastMessage = e instanceof Error ? e.message : String(e);
    }
    if (attempt < REPORT_ATTEMPTS) {
      await new Promise((resolve) => setTimeout(resolve, attempt * 100));
    }
  }
  logError(`Failed to report prune action ${request.action_token}: ${lastMessage}`);
  return false;
}

function claimAction(action: PruneActionRequired): Promise<boolean> {
  return reportWithRetry({
    phase: "claim",
    run_id: action.run_id,
    action_token: action.action_token,
  });
}

function assertCurrent(generation: number): void {
  if (generation !== actionGeneration) throw new Error("Cleanup action was cancelled before Steam mutation");
}

async function executePruneAction(action: PruneActionRequired, generation: number): Promise<void> {
  let claimed = false;
  const claim = async (): Promise<boolean> => {
    if (claimed) return true;
    claimed = await claimAction(action);
    return claimed;
  };
  let report: CompletePruneActionRequest;
  try {
    assertCurrent(generation);
    if (action.action === "capture_shortcut_snapshot") {
      if (!(await claim())) return;
      assertCurrent(generation);
      report = {
        phase: "complete",
        run_id: action.run_id,
        action_token: action.action_token,
        success: true,
        message: "Steam shortcut state captured.",
        snapshot: await captureShortcutSnapshot(action.app_id),
      };
    } else if (action.action === "repoint_shortcut") {
      const details = await getAppDetails(action.app_id);
      if (!isRomMShortcutDetails(details)) throw new Error("The live shortcut is not owned by RomM Sync");
      assertCurrent(generation);
      if (!(await claim())) return;
      assertCurrent(generation);
      const result: SwitchVersionSuccess = {
        success: true,
        app_id: action.app_id,
        rom_id: action.target_rom_id,
        target_installed: action.target_installed,
        launch_options: action.launch_options,
      };
      if (!(await applyCommittedVersionSwitch(result))) {
        throw new Error("Steam did not confirm the target launch command");
      }
      report = {
        phase: "complete",
        run_id: action.run_id,
        action_token: action.action_token,
        success: true,
        message: "Shortcut repointed and launch command confirmed.",
      };
    } else {
      const details = await getAppDetails(action.app_id);
      if (!isRomMShortcutDetails(details)) throw new Error("The live shortcut is not owned by RomM Sync");
      assertCurrent(generation);
      if (!(await claim())) return;
      assertCurrent(generation);
      if (!(await removeShortcutConfirmed(action.app_id, 3000, true))) {
        throw new Error("Steam did not confirm owned-shortcut removal");
      }
      report = {
        phase: "complete",
        run_id: action.run_id,
        action_token: action.action_token,
        success: true,
        message: "Steam confirmed shortcut removal.",
      };
    }
  } catch (e) {
    report = {
      phase: "complete",
      run_id: action.run_id,
      action_token: action.action_token,
      success: false,
      reason: "steam_action_failed",
      message: e instanceof Error ? e.message : String(e),
    };
  }
  if (!(await claim())) return;
  await reportWithRetry(report);
}

export function handlePruneAction(value: PruneActionRequired): Promise<void> {
  if (!isPruneAction(value)) {
    logError("Ignored an invalid prune action event.");
    return Promise.resolve();
  }
  if (handledTokens.has(value.action_token)) return Promise.resolve();
  handledTokens.add(value.action_token);
  const generation = actionGeneration;
  const queued = actionQueue.then(() => executePruneAction(value, generation));
  actionQueue = queued.catch(() => {});
  return queued;
}

export function cancelPruneActions(): void {
  actionGeneration++;
  handledTokens.clear();
}
