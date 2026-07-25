import { beforeEach, describe, expect, it, vi } from "vitest";
import * as backend from "../api/backend";
import { cancelPruneActions, handlePruneAction } from "./pruneActions";
import {
  getAppDetails,
  isRomMShortcutDetails,
  removeShortcutConfirmed,
  setLaunchOptionsConfirmed,
} from "./steamShortcuts";

vi.mock("./steamShortcuts", () => ({
  getAppDetails: vi.fn(),
  isRomMShortcutDetails: vi.fn(),
  removeShortcutConfirmed: vi.fn(),
  setLaunchOptionsConfirmed: vi.fn(),
}));

describe("handlePruneAction", () => {
  beforeEach(() => {
    vi.mocked(backend.reportPruneAction).mockReset();
    vi.mocked(backend.switchVersion).mockReset();
    vi.mocked(backend.fetchCoverBase64).mockReset();
    vi.mocked(getAppDetails).mockReset();
    vi.mocked(isRomMShortcutDetails).mockReset();
    vi.mocked(removeShortcutConfirmed).mockReset();
    vi.mocked(setLaunchOptionsConfirmed).mockReset();
    vi.mocked(backend.reportPruneAction).mockResolvedValue({ success: true, message: "accepted" });
    vi.mocked(isRomMShortcutDetails).mockReturnValue(true);
  });

  it("captures bounded Steam state without file bytes", async () => {
    vi.mocked(getAppDetails).mockResolvedValue({
      strDisplayName: "Removed Game",
      strShortcutExe: "/plugin/bin/rom-launcher",
      strShortcutStartDir: "/plugin",
      strLaunchOptions: "launch-command",
    });
    vi.stubGlobal("collectionStore", {
      userCollections: [
        { id: "favorites", displayName: "Favorites", apps: new Set([9001]) },
        { id: "other", displayName: "Other", apps: new Set([42]) },
      ],
    });
    vi.stubGlobal("appStore", {
      GetAppOverviewByAppID: vi.fn(() => ({
        minutes_playtime_forever: 120,
        minutes_playtime_last_two_weeks: 15,
        rt_last_time_played: 1234,
      })),
    });

    await handlePruneAction({
      run_id: "run-1",
      action_token: "token-1",
      action: "capture_shortcut_snapshot",
      app_id: 9001,
    });

    expect(backend.reportPruneAction).toHaveBeenCalledWith({
      phase: "complete",
      run_id: "run-1",
      action_token: "token-1",
      success: true,
      message: "Steam shortcut state captured.",
      snapshot: {
        app_id: 9001,
        name: "Removed Game",
        exe: "/plugin/bin/rom-launcher",
        start_dir: "/plugin",
        launch_options: "launch-command",
        minutes_playtime_forever: 120,
        minutes_playtime_last_two_weeks: 15,
        last_played: 1234,
        collections: [{ id: "favorites", name: "Favorites" }],
      },
    });
    expect(JSON.stringify(vi.mocked(backend.reportPruneAction).mock.calls[0]?.[0])).not.toContain("base64");
  });

  it("fails closed when complete collection state exceeds the snapshot budget", async () => {
    vi.mocked(getAppDetails).mockResolvedValue({
      strDisplayName: "é".repeat(2048),
      strShortcutExe: "é".repeat(2048),
      strShortcutStartDir: "é".repeat(2048),
      strLaunchOptions: "é".repeat(2048),
    });
    vi.stubGlobal("collectionStore", {
      userCollections: Array.from({ length: 256 }, (_, index) => ({
        id: `id-${index}-${"é".repeat(512)}`,
        displayName: `name-${index}-${"é".repeat(512)}`,
        apps: new Set([9001]),
      })),
    });
    vi.stubGlobal("appStore", { GetAppOverviewByAppID: vi.fn(() => ({})) });

    await handlePruneAction({
      run_id: "run-1",
      action_token: "token-large",
      action: "capture_shortcut_snapshot",
      app_id: 9001,
    });

    const completion = vi
      .mocked(backend.reportPruneAction)
      .mock.calls.map(([request]) => request)
      .find((request) => request.phase === "complete");
    expect(completion).toMatchObject({
      phase: "complete",
      success: false,
      reason: "steam_action_failed",
      message: expect.stringContaining("too large"),
    });
  });

  it("fails closed when Steam playtime state is unavailable", async () => {
    vi.mocked(getAppDetails).mockResolvedValue({
      strDisplayName: "Removed Game",
      strShortcutExe: "/plugin/bin/rom-launcher",
      strShortcutStartDir: "/plugin",
      strLaunchOptions: "launch-command",
    });
    vi.stubGlobal("collectionStore", { userCollections: [] });
    vi.stubGlobal("appStore", undefined);

    await handlePruneAction({
      run_id: "run-1",
      action_token: "token-incomplete",
      action: "capture_shortcut_snapshot",
      app_id: 9001,
    });

    expect(
      vi
        .mocked(backend.reportPruneAction)
        .mock.calls.map(([request]) => request)
        .find((request) => request.phase === "complete"),
    ).toMatchObject({ success: false, message: expect.stringContaining("unavailable") });
  });

  it("confirms the exact repoint launch command and artwork before acknowledging", async () => {
    vi.mocked(backend.switchVersion).mockResolvedValue({
      success: true,
      app_id: 9001,
      rom_id: 8,
      target_installed: true,
      launch_options: "target-command",
    });
    vi.mocked(setLaunchOptionsConfirmed).mockResolvedValue(true);
    vi.mocked(backend.fetchCoverBase64).mockResolvedValue({ base64: "cover" });
    const setArtwork = vi.fn().mockResolvedValue(undefined);
    vi.stubGlobal("SteamClient", { Apps: { SetCustomArtworkForApp: setArtwork } });
    vi.mocked(getAppDetails).mockResolvedValue({ strShortcutExe: "/plugin/bin/rom-launcher" });
    const changed = vi.fn();
    globalThis.addEventListener("romm_data_changed", changed);

    await handlePruneAction({
      run_id: "run-1",
      action_token: "token-2",
      action: "repoint_shortcut",
      app_id: 9001,
      target_rom_id: 8,
      launch_options: "target-command",
      target_installed: true,
    });

    expect(backend.switchVersion).not.toHaveBeenCalled();
    expect(setLaunchOptionsConfirmed).toHaveBeenCalledWith(9001, "target-command");
    expect(setArtwork).toHaveBeenCalledWith(9001, "cover", "png", 0);
    expect(changed).toHaveBeenCalledWith(
      expect.objectContaining({ detail: { type: "version_switched", app_id: 9001, rom_id: 8 } }),
    );
    expect(backend.reportPruneAction).toHaveBeenCalledWith({
      phase: "complete",
      run_id: "run-1",
      action_token: "token-2",
      success: true,
      message: "Shortcut repointed and launch command confirmed.",
    });
    globalThis.removeEventListener("romm_data_changed", changed);
  });

  it("reports shortcut-removal confirmation failure instead of finalizing", async () => {
    vi.mocked(removeShortcutConfirmed).mockResolvedValue(false);

    await handlePruneAction({
      run_id: "run-1",
      action_token: "token-3",
      action: "remove_shortcut",
      app_id: 9001,
    });

    expect(backend.reportPruneAction).toHaveBeenCalledWith({
      phase: "complete",
      run_id: "run-1",
      action_token: "token-3",
      success: false,
      reason: "steam_action_failed",
      message: "Steam did not confirm owned-shortcut removal",
    });
  });

  it("logs a rejected action report after preserving the failure payload", async () => {
    vi.mocked(removeShortcutConfirmed).mockResolvedValue(false);
    vi.mocked(backend.reportPruneAction).mockRejectedValue(new Error("bridge offline"));
    const log = vi.spyOn(backend, "logError").mockImplementation(() => {});

    await handlePruneAction({
      run_id: "run-1",
      action_token: "token-4",
      action: "remove_shortcut",
      app_id: 9001,
    });

    expect(log).toHaveBeenCalledWith(expect.stringContaining("token-4"));
    expect(log).toHaveBeenCalledWith(expect.stringContaining("bridge offline"));
  });

  it("deduplicates duplicate action delivery before Steam mutation", async () => {
    vi.mocked(removeShortcutConfirmed).mockResolvedValue(true);
    const action = {
      run_id: "run-1",
      action_token: "token-duplicate",
      action: "remove_shortcut" as const,
      app_id: 9001,
    };

    await Promise.all([handlePruneAction(action), handlePruneAction(action)]);

    expect(removeShortcutConfirmed).toHaveBeenCalledTimes(1);
    expect(backend.reportPruneAction).toHaveBeenCalledTimes(2);
  });

  it("does not touch Steam when the backend rejects the pre-action claim", async () => {
    vi.mocked(backend.reportPruneAction).mockResolvedValue({
      success: false,
      reason: "stale_action",
      message: "expired",
    });

    await handlePruneAction({
      run_id: "run-old",
      action_token: "token-stale",
      action: "remove_shortcut",
      app_id: 9001,
    });

    expect(removeShortcutConfirmed).not.toHaveBeenCalled();
    expect(backend.reportPruneAction).toHaveBeenCalledTimes(1);
  });

  it("retries only the report after a successful Steam mutation", async () => {
    vi.mocked(removeShortcutConfirmed).mockResolvedValue(true);
    vi.mocked(backend.reportPruneAction)
      .mockResolvedValueOnce({ success: true, message: "claimed" })
      .mockRejectedValueOnce(new Error("bridge reset"))
      .mockResolvedValueOnce({ success: true, message: "accepted" });

    await handlePruneAction({
      run_id: "run-1",
      action_token: "token-retry",
      action: "remove_shortcut",
      app_id: 9001,
    });

    expect(removeShortcutConfirmed).toHaveBeenCalledTimes(1);
    expect(backend.reportPruneAction).toHaveBeenCalledTimes(3);
    expect(vi.mocked(backend.reportPruneAction).mock.calls[1]?.[0]).toEqual(
      vi.mocked(backend.reportPruneAction).mock.calls[2]?.[0],
    );
  });

  it("cancels queued actions before they can mutate Steam", async () => {
    let resolveClaim: ((value: { success: true; message: string }) => void) | undefined;
    vi.mocked(backend.reportPruneAction).mockImplementation(
      () =>
        new Promise((resolve) => {
          resolveClaim = resolve;
        }),
    );
    const pending = handlePruneAction({
      run_id: "run-1",
      action_token: "token-cancelled",
      action: "remove_shortcut",
      app_id: 9001,
    });
    await vi.waitFor(() => expect(resolveClaim).toBeDefined());

    cancelPruneActions();
    resolveClaim?.({ success: true, message: "claimed" });
    vi.mocked(backend.reportPruneAction).mockResolvedValue({ success: true, message: "accepted" });
    await pending;

    expect(removeShortcutConfirmed).not.toHaveBeenCalled();
  });

  it("rejects an unknown runtime discriminant without claiming or mutating", async () => {
    const log = vi.spyOn(backend, "logError").mockImplementation(() => {});

    await handlePruneAction({
      run_id: "run-1",
      action_token: "token-unknown",
      action: "future_destructive_action",
      app_id: 9001,
    } as never);

    expect(backend.reportPruneAction).not.toHaveBeenCalled();
    expect(removeShortcutConfirmed).not.toHaveBeenCalled();
    expect(log).toHaveBeenCalledWith("Ignored an invalid prune action event.");
  });
});
