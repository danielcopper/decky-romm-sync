import { describe, it, expect, beforeEach, vi } from "vitest";
import * as backend from "../api/backend";
import { applyArtwork } from "./artwork";

describe("applyArtwork", () => {
  beforeEach(() => {
    vi.resetAllMocks();
    vi.stubGlobal("SteamClient", {
      Apps: {
        SetCustomArtworkForApp: vi.fn().mockResolvedValue(undefined),
      },
    });
    vi.mocked(backend.saveShortcutIcon).mockResolvedValue({ success: true });
  });

  it("requests all four SGDB asset types for the rom id", async () => {
    vi.mocked(backend.getSgdbArtworkBase64).mockResolvedValue({ base64: null, no_api_key: false });
    await applyArtwork(42, 5000);
    expect(vi.mocked(backend.getSgdbArtworkBase64)).toHaveBeenCalledWith(42, 1);
    expect(vi.mocked(backend.getSgdbArtworkBase64)).toHaveBeenCalledWith(42, 2);
    expect(vi.mocked(backend.getSgdbArtworkBase64)).toHaveBeenCalledWith(42, 3);
    expect(vi.mocked(backend.getSgdbArtworkBase64)).toHaveBeenCalledWith(42, 4);
  });

  it("returns -1 when any asset reports no_api_key", async () => {
    vi.mocked(backend.getSgdbArtworkBase64)
      .mockResolvedValueOnce({ base64: null, no_api_key: false })
      .mockResolvedValueOnce({ base64: null, no_api_key: true })
      .mockResolvedValueOnce({ base64: null, no_api_key: false })
      .mockResolvedValueOnce({ base64: null, no_api_key: false });
    await expect(applyArtwork(42, 5000)).resolves.toBe(-1);
    // Short-circuits before writing any artwork.
    expect(vi.mocked(SteamClient.Apps.SetCustomArtworkForApp)).not.toHaveBeenCalled();
    expect(vi.mocked(backend.saveShortcutIcon)).not.toHaveBeenCalled();
  });

  it("maps types 1-3 to SetCustomArtworkForApp and type 4 to saveShortcutIcon, returns count", async () => {
    vi.mocked(backend.getSgdbArtworkBase64)
      .mockResolvedValueOnce({ base64: "AA==", no_api_key: false })
      .mockResolvedValueOnce({ base64: "BB==", no_api_key: false })
      .mockResolvedValueOnce({ base64: "CC==", no_api_key: false })
      .mockResolvedValueOnce({ base64: "DD==", no_api_key: false });
    await expect(applyArtwork(42, 5000)).resolves.toBe(4);
    expect(vi.mocked(SteamClient.Apps.SetCustomArtworkForApp)).toHaveBeenCalledWith(5000, "AA==", "png", 1);
    expect(vi.mocked(SteamClient.Apps.SetCustomArtworkForApp)).toHaveBeenCalledWith(5000, "BB==", "png", 2);
    expect(vi.mocked(SteamClient.Apps.SetCustomArtworkForApp)).toHaveBeenCalledWith(5000, "CC==", "png", 3);
    expect(vi.mocked(backend.saveShortcutIcon)).toHaveBeenCalledWith(5000, "DD==");
  });

  it("returns 0 and writes nothing when all assets are null", async () => {
    vi.mocked(backend.getSgdbArtworkBase64).mockResolvedValue({ base64: null, no_api_key: false });
    await expect(applyArtwork(42, 5000)).resolves.toBe(0);
    expect(vi.mocked(SteamClient.Apps.SetCustomArtworkForApp)).not.toHaveBeenCalled();
    expect(vi.mocked(backend.saveShortcutIcon)).not.toHaveBeenCalled();
  });

  it("counts only the assets that returned base64", async () => {
    vi.mocked(backend.getSgdbArtworkBase64)
      .mockResolvedValueOnce({ base64: "AA==", no_api_key: false })
      .mockResolvedValueOnce({ base64: null, no_api_key: false })
      .mockResolvedValueOnce({ base64: "CC==", no_api_key: false })
      .mockResolvedValueOnce({ base64: null, no_api_key: false });
    await expect(applyArtwork(42, 5000)).resolves.toBe(2);
    expect(vi.mocked(SteamClient.Apps.SetCustomArtworkForApp)).toHaveBeenCalledTimes(2);
    expect(vi.mocked(backend.saveShortcutIcon)).not.toHaveBeenCalled();
  });

  it("per-asset fetch rejection is swallowed → treated as null (returns 0)", async () => {
    vi.mocked(backend.getSgdbArtworkBase64).mockRejectedValue(new Error("net"));
    await expect(applyArtwork(42, 5000)).resolves.toBe(0);
    expect(vi.mocked(SteamClient.Apps.SetCustomArtworkForApp)).not.toHaveBeenCalled();
  });
});
