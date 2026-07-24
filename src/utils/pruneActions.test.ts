import { beforeEach, describe, expect, it, vi } from "vitest";
import * as backend from "../api/backend";
import { handlePruneAction } from "./pruneActions";
import { getAppDetails, removeShortcutConfirmed, setLaunchOptionsConfirmed } from "./steamShortcuts";

vi.mock("./steamShortcuts", () => ({
  getAppDetails: vi.fn(),
  removeShortcutConfirmed: vi.fn(),
  setLaunchOptionsConfirmed: vi.fn(),
}));

describe("handlePruneAction", () => {
  beforeEach(() => {
    vi.mocked(backend.reportPruneAction).mockReset();
    vi.mocked(backend.switchVersion).mockReset();
    vi.mocked(backend.fetchCoverBase64).mockReset();
    vi.mocked(getAppDetails).mockReset();
    vi.mocked(removeShortcutConfirmed).mockReset();
    vi.mocked(setLaunchOptionsConfirmed).mockReset();
    vi.mocked(backend.reportPruneAction).mockResolvedValue({ success: true, message: "accepted" });
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

  it("trims large Unicode collection state below the backend snapshot budget", async () => {
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
    vi.stubGlobal("appStore", { GetAppOverviewByAppID: vi.fn(() => null) });

    await handlePruneAction({
      run_id: "run-1",
      action_token: "token-large",
      action: "capture_shortcut_snapshot",
      app_id: 9001,
    });

    const snapshot = vi.mocked(backend.reportPruneAction).mock.calls[0]?.[0].snapshot;
    expect(snapshot).toBeDefined();
    expect(snapshot?.collections.length).toBeLessThan(256);
    let asciiBytes = 0;
    for (const char of JSON.stringify(snapshot)) {
      const codePoint = char.codePointAt(0) ?? 0;
      asciiBytes += codePoint <= 0x7f ? 1 : codePoint <= 0xffff ? 6 : 12;
    }
    expect(asciiBytes).toBeLessThanOrEqual(56 * 1024);
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

    await handlePruneAction({
      run_id: "run-1",
      action_token: "token-2",
      action: "repoint_shortcut",
      app_id: 9001,
      target_rom_id: 8,
      allow_stranded: true,
    });

    expect(backend.switchVersion).toHaveBeenCalledWith(9001, 8, true);
    expect(setLaunchOptionsConfirmed).toHaveBeenCalledWith(9001, "target-command");
    expect(setArtwork).toHaveBeenCalledWith(9001, "cover", "png", 0);
    expect(backend.reportPruneAction).toHaveBeenCalledWith({
      run_id: "run-1",
      action_token: "token-2",
      success: true,
      message: "Shortcut repointed and launch command confirmed.",
    });
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
      run_id: "run-1",
      action_token: "token-3",
      success: false,
      reason: "steam_action_failed",
      message: "Steam did not confirm shortcut removal",
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
});
