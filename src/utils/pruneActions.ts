import { logError, reportPruneAction } from "../api/backend";
import type {
  CompletePruneActionRequest,
  PruneSteamSnapshot,
  ReportPruneActionRequest,
  SwitchVersionSuccess,
} from "../api/backend";
import {
  getAppDetails,
  isRomMShortcutDetails,
  removeShortcutConfirmedOutcome,
  setLaunchOptionsConfirmed,
} from "./steamShortcuts";
import { withTimeout } from "./withTimeout";

type PruneAction =
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
  | {
      run_id: string;
      action_token: string;
      action: "remove_shortcut";
      app_id: number;
      expected_snapshot?: PruneSteamSnapshot;
    };

export type PruneActionRequired = PruneAction & { preview_id: string };

const SNAPSHOT_BUDGET_BYTES = 56 * 1024;
const REPORT_ATTEMPTS = 3;
const REPORT_TIMEOUT_MS = 5000;
const handledTokens = new Set<string>();
let actionQueue: Promise<void> = Promise.resolve();
let actionGeneration = 0;

function asciiJsonSize(value: unknown): number {
  let bytes = 0;
  for (const char of JSON.stringify(value)) {
    const codePoint = char.codePointAt(0) ?? 0;
    if (codePoint <= 0x7f) bytes += 1;
    else if (codePoint <= 0xffff) bytes += 6;
    else bytes += 12;
  }
  return bytes;
}

function isPruneAction(value: unknown): value is PruneAction {
  if (!value || typeof value !== "object") return false;
  const item = value as Record<string, unknown>;
  if (typeof item.run_id !== "string" || typeof item.action_token !== "string" || typeof item.app_id !== "number") {
    return false;
  }
  if (item.action === "capture_shortcut_snapshot") return true;
  if (item.action === "remove_shortcut") {
    return (
      item.expected_snapshot === undefined ||
      (item.expected_snapshot !== null && typeof item.expected_snapshot === "object")
    );
  }
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
    throw new TypeError("Steam shortcut, collection, or playtime state was unavailable");
  }
  const liveApps = collectionStore.deckDesktopApps?.apps;
  if (!liveApps) throw new Error("Steam shortcut store was unavailable");
  if (!liveApps.has(appId)) throw new Error("Steam shortcut is absent");
  const details = await getAppDetails(appId);
  if (!isRomMShortcutDetails(details)) throw new Error("The live shortcut is not owned by Tender");
  const overview = appStore.GetAppOverviewByAppID(appId);
  if (!overview) throw new Error("Steam shortcut playtime state was unavailable");
  const collections = collectionStore.userCollections
    .filter((collection) => collection.apps.has(appId))
    .map((collection) => ({
      id: requireString(collection.id, "collection id"),
      name: requireString(collection.displayName, "collection name"),
    }))
    .sort((left, right) => left.id.localeCompare(right.id));
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
      const result = await withTimeout(reportPruneAction(request), REPORT_TIMEOUT_MS);
      if (result.success) return true;
      lastMessage = result.message;
      if (result.reason === "stale_action" || result.reason === "local_state_changed") break;
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

function claimAction(action: PruneAction): Promise<boolean> {
  return reportWithRetry({
    phase: "claim",
    run_id: action.run_id,
    action_token: action.action_token,
    action: action.action,
    app_id: action.app_id,
    target_rom_id: action.action === "repoint_shortcut" ? action.target_rom_id : null,
  });
}

function assertCurrent(generation: number): void {
  if (generation !== actionGeneration) throw new Error("Cleanup action was cancelled before Steam mutation");
}

/**
 * Whether a Steam mutation has already been issued for this action. Mutable
 * across the branch helpers because a failure *after* the mutation went out has
 * to report it as attempted — an outcome lost in transit is ambiguous, never a
 * clean failure.
 */
interface SteamMutationAttempt {
  issued: boolean;
}

function shortcutAbsentReport(action: PruneAction): CompletePruneActionRequest {
  return {
    phase: "complete",
    run_id: action.run_id,
    action_token: action.action_token,
    success: true,
    message: "Steam confirmed the shortcut is already absent.",
    shortcut_absent: true,
  };
}

async function captureSnapshotReport(action: PruneAction): Promise<CompletePruneActionRequest> {
  try {
    return {
      phase: "complete",
      run_id: action.run_id,
      action_token: action.action_token,
      success: true,
      message: "Steam shortcut state captured.",
      snapshot: await captureShortcutSnapshot(action.app_id),
    };
  } catch (error) {
    if (error instanceof Error && error.message === "Steam shortcut is absent") return shortcutAbsentReport(action);
    throw error;
  }
}

async function repointShortcutReport(
  action: Extract<PruneAction, { action: "repoint_shortcut" }>,
  generation: number,
  attempt: SteamMutationAttempt,
): Promise<CompletePruneActionRequest> {
  const details = await getAppDetails(action.app_id);
  if (!isRomMShortcutDetails(details)) throw new Error("The live shortcut is not owned by Tender");
  assertCurrent(generation);
  const result: SwitchVersionSuccess = {
    success: true,
    app_id: action.app_id,
    rom_id: action.target_rom_id,
    target_installed: action.target_installed,
    launch_options: action.launch_options,
  };
  attempt.issued = true;
  if (!(await setLaunchOptionsConfirmed(result.app_id, result.launch_options))) {
    throw new Error("Steam did not confirm the target launch command");
  }
  return {
    phase: "complete",
    run_id: action.run_id,
    action_token: action.action_token,
    success: true,
    message: "Shortcut repointed and launch command confirmed.",
  };
}

async function removeShortcutReport(
  action: Extract<PruneAction, { action: "remove_shortcut" }>,
  generation: number,
  attempt: SteamMutationAttempt,
): Promise<CompletePruneActionRequest> {
  let fresh: PruneSteamSnapshot;
  try {
    fresh = await captureShortcutSnapshot(action.app_id);
  } catch (error) {
    if (error instanceof Error && error.message === "Steam shortcut is absent") return shortcutAbsentReport(action);
    throw error;
  }
  if (action.expected_snapshot && JSON.stringify(fresh) !== JSON.stringify(action.expected_snapshot)) {
    throw new Error("Steam shortcut state changed after recovery was sealed");
  }
  assertCurrent(generation);
  const removal = await removeShortcutConfirmedOutcome(action.app_id, 3000, true);
  attempt.issued = removal.status === "attempted_unconfirmed" || removal.status === "confirmed";
  if (removal.status !== "confirmed") {
    throw new Error("Steam did not confirm owned-shortcut removal");
  }
  return {
    phase: "complete",
    run_id: action.run_id,
    action_token: action.action_token,
    success: true,
    message: "Steam confirmed shortcut removal.",
  };
}

async function executePruneAction(action: PruneAction, generation: number): Promise<void> {
  let claimed = false;
  const attempt: SteamMutationAttempt = { issued: false };
  const claim = async (): Promise<boolean> => {
    if (claimed) return true;
    claimed = await claimAction(action);
    return claimed;
  };
  let report: CompletePruneActionRequest;
  try {
    assertCurrent(generation);
    if (!(await claim())) return;
    assertCurrent(generation);
    if (action.action === "capture_shortcut_snapshot") {
      report = await captureSnapshotReport(action);
    } else if (action.action === "repoint_shortcut") {
      report = await repointShortcutReport(action, generation, attempt);
    } else {
      report = await removeShortcutReport(action, generation, attempt);
    }
  } catch (e) {
    report = {
      phase: "complete",
      run_id: action.run_id,
      action_token: action.action_token,
      success: false,
      reason: "steam_action_failed",
      message: e instanceof Error ? e.message : String(e),
      ...(attempt.issued ? { mutation_attempted: true } : {}),
    };
  }
  if (!(await claim())) return;
  await reportWithRetry(report);
}

export function handlePruneAction(value: PruneAction): Promise<void> {
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
  actionQueue = Promise.resolve();
}
