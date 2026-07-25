import { describe, it, expect, beforeEach, vi } from "vitest";
import * as backend from "../api/backend";
import { applyArtwork, cancelArtworkApply } from "./artwork";

describe("applyArtwork", () => {
  beforeEach(() => {
    vi.resetAllMocks();
    vi.stubGlobal("SteamClient", {
      Apps: {
        SetCustomArtworkForApp: vi.fn().mockResolvedValue(undefined),
        SetShortcutIcon: vi.fn(),
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
    vi.mocked(backend.saveShortcutIcon).mockResolvedValue({ success: true, icon_path: "/grid/5000_icon.png" });
    await expect(applyArtwork(42, 5000)).resolves.toBe(4);
    expect(vi.mocked(SteamClient.Apps.SetCustomArtworkForApp)).toHaveBeenCalledWith(5000, "AA==", "png", 1);
    expect(vi.mocked(SteamClient.Apps.SetCustomArtworkForApp)).toHaveBeenCalledWith(5000, "BB==", "png", 2);
    expect(vi.mocked(SteamClient.Apps.SetCustomArtworkForApp)).toHaveBeenCalledWith(5000, "CC==", "png", 3);
    expect(vi.mocked(backend.saveShortcutIcon)).toHaveBeenCalledWith(5000, "DD==");
    // The returned grid path is applied to the shortcut live via SteamClient.
    expect(vi.mocked(SteamClient.Apps.SetShortcutIcon)).toHaveBeenCalledWith(5000, "/grid/5000_icon.png");
  });

  it("holds every SGDB fetch lease through the final Steam artwork write", async () => {
    const write = deferred<void>();
    vi.mocked(backend.getSgdbArtworkBase64)
      .mockResolvedValueOnce({ base64: "AA==", no_api_key: false, prune_lease_token: "lease-1" })
      .mockResolvedValueOnce({ base64: null, no_api_key: false, prune_lease_token: "lease-2" })
      .mockResolvedValueOnce({ base64: null, no_api_key: false, prune_lease_token: "lease-3" })
      .mockResolvedValueOnce({ base64: null, no_api_key: false, prune_lease_token: "lease-4" });
    vi.mocked(backend.releasePruneConflictLease).mockResolvedValue({ success: true, message: "released" });
    vi.mocked(SteamClient.Apps.SetCustomArtworkForApp).mockImplementationOnce(() => write.promise);

    const applying = applyArtwork(42, 5000);
    await vi.waitFor(() => expect(SteamClient.Apps.SetCustomArtworkForApp).toHaveBeenCalled());
    expect(backend.releasePruneConflictLease).not.toHaveBeenCalled();

    write.resolve(undefined);
    await expect(applying).resolves.toBe(1);
    expect(
      vi
        .mocked(backend.releasePruneConflictLease)
        .mock.calls.map(([token]) => token)
        .sort(),
    ).toEqual(["lease-1", "lease-2", "lease-3", "lease-4"]);
  });

  it("component cancellation retains artwork leases until a pending Steam write settles", async () => {
    const write = deferred<void>();
    vi.mocked(backend.getSgdbArtworkBase64).mockResolvedValue({
      base64: "AA==",
      no_api_key: false,
      prune_lease_token: "artwork-lease",
    });
    vi.mocked(backend.releasePruneConflictLease).mockResolvedValue({ success: true, message: "released" });
    vi.mocked(SteamClient.Apps.SetCustomArtworkForApp).mockImplementationOnce(() => write.promise);

    const applying = applyArtwork(42, 5000);
    await vi.waitFor(() => expect(SteamClient.Apps.SetCustomArtworkForApp).toHaveBeenCalled());
    const cancelling = cancelArtworkApply(5000);
    await Promise.resolve();
    expect(backend.releasePruneConflictLease).not.toHaveBeenCalledWith("artwork-lease");

    write.resolve(undefined);
    await applying;
    await cancelling;
    expect(backend.releasePruneConflictLease).toHaveBeenCalledWith("artwork-lease");
    expect(SteamClient.Apps.SetCustomArtworkForApp).toHaveBeenCalledTimes(1);
  });

  it("does not call SetShortcutIcon when saveShortcutIcon reports failure", async () => {
    vi.mocked(backend.getSgdbArtworkBase64)
      .mockResolvedValueOnce({ base64: null, no_api_key: false })
      .mockResolvedValueOnce({ base64: null, no_api_key: false })
      .mockResolvedValueOnce({ base64: null, no_api_key: false })
      .mockResolvedValueOnce({ base64: "DD==", no_api_key: false });
    vi.mocked(backend.saveShortcutIcon).mockResolvedValue({ success: false });
    // Only the icon asset had base64, and it failed → count is 0 (the failed icon
    // is NOT counted). Before the L4 fix `applied++` ran regardless and returned 1.
    await expect(applyArtwork(42, 5000)).resolves.toBe(0);
    expect(vi.mocked(backend.saveShortcutIcon)).toHaveBeenCalledWith(5000, "DD==");
    expect(vi.mocked(SteamClient.Apps.SetShortcutIcon)).not.toHaveBeenCalled();
  });

  it("does not call SetShortcutIcon when saveShortcutIcon returns no icon_path", async () => {
    vi.mocked(backend.getSgdbArtworkBase64)
      .mockResolvedValueOnce({ base64: null, no_api_key: false })
      .mockResolvedValueOnce({ base64: null, no_api_key: false })
      .mockResolvedValueOnce({ base64: null, no_api_key: false })
      .mockResolvedValueOnce({ base64: "DD==", no_api_key: false });
    vi.mocked(backend.saveShortcutIcon).mockResolvedValue({ success: true });
    // Icon succeeded but returned no path → not applied, so not counted → 0 (#L4).
    await expect(applyArtwork(42, 5000)).resolves.toBe(0);
    expect(vi.mocked(backend.saveShortcutIcon)).toHaveBeenCalledWith(5000, "DD==");
    expect(vi.mocked(SteamClient.Apps.SetShortcutIcon)).not.toHaveBeenCalled();
  });

  it("counts a successful hero but NOT a failed icon in the same apply (#L4)", async () => {
    vi.mocked(backend.getSgdbArtworkBase64)
      .mockResolvedValueOnce({ base64: "AA==", no_api_key: false }) // hero → applied
      .mockResolvedValueOnce({ base64: null, no_api_key: false })
      .mockResolvedValueOnce({ base64: null, no_api_key: false })
      .mockResolvedValueOnce({ base64: "DD==", no_api_key: false }); // icon → fails
    vi.mocked(backend.saveShortcutIcon).mockResolvedValue({ success: false });
    // Hero counts (1), the failed icon does not → 1, not 2.
    await expect(applyArtwork(42, 5000)).resolves.toBe(1);
    expect(vi.mocked(SteamClient.Apps.SetShortcutIcon)).not.toHaveBeenCalled();
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

type Art = { base64: string | null; no_api_key?: boolean };

function deferred<T>(): { promise: Promise<T>; resolve: (value: T) => void } {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((r) => {
    resolve = r;
  });
  return { promise, resolve };
}

describe("applyArtwork — newest-apply-wins race guard", () => {
  beforeEach(() => {
    vi.resetAllMocks();
    vi.stubGlobal("SteamClient", {
      Apps: {
        SetCustomArtworkForApp: vi.fn().mockResolvedValue(undefined),
        SetShortcutIcon: vi.fn(),
      },
    });
    // Icon succeeds WITH a path so it counts toward the applied total (these tests
    // exercise all four assets applying) — a success without a path is not counted (#L4).
    vi.mocked(backend.saveShortcutIcon).mockResolvedValue({ success: true, icon_path: "/grid/icon.png" });
  });

  it("a superseded in-flight apply writes nothing — only the newer apply's art lands for that appId", async () => {
    const appId = 5000;
    const slow = deferred<Art>();
    // Apply A (rom 42) hangs on `slow` for every asset; apply B (rom 99) resolves
    // fast with distinct art and completes its writes first.
    vi.mocked(backend.getSgdbArtworkBase64).mockImplementation((romId: number): Promise<Art> => {
      if (romId === 42) return slow.promise;
      return Promise.resolve({ base64: "BB==", no_api_key: false });
    });

    // A starts first (claims the older generation), B starts second for the SAME appId.
    const aPromise = applyArtwork(42, appId);
    const bPromise = applyArtwork(99, appId);
    await bPromise;

    // B's art is on the tile.
    const write = vi.mocked(SteamClient.Apps.SetCustomArtworkForApp);
    expect(write).toHaveBeenCalledWith(appId, "BB==", "png", 1);

    // Now let A's slow fetches finally resolve with the OLD art.
    slow.resolve({ base64: "AA==", no_api_key: false });
    await aPromise;

    // Assert on call CONTENTS, not counts: every write carries B's art, none carry
    // A's — the stale apply performed no writes after being superseded.
    expect(write.mock.calls.every((c) => c[1] === "BB==")).toBe(true);
    expect(write.mock.calls.some((c) => c[1] === "AA==")).toBe(false);
    // Exactly one hero (assetType 1) write, and it is B's.
    expect(write.mock.calls.filter((c) => c[3] === 1)).toEqual([[appId, "BB==", "png", 1]]);
    // The icon path (saveShortcutIcon) only ever received B's art, never A's.
    expect(vi.mocked(backend.saveShortcutIcon).mock.calls.some((c) => c[1] === "AA==")).toBe(false);
    // A resolved to the count it managed to write (0) rather than throwing.
    await expect(aPromise).resolves.toBe(0);
  });

  it("sequential applies for the same appId both write (no over-suppression)", async () => {
    const appId = 5000;
    vi.mocked(backend.getSgdbArtworkBase64).mockImplementation((romId: number): Promise<Art> =>
      Promise.resolve({ base64: romId === 1 ? "AA==" : "BB==", no_api_key: false }),
    );

    await expect(applyArtwork(1, appId)).resolves.toBe(4); // A completes fully
    await expect(applyArtwork(2, appId)).resolves.toBe(4); // then B completes fully

    const write = vi.mocked(SteamClient.Apps.SetCustomArtworkForApp);
    // Both applies wrote their hero art — the guard only suppresses a call that a
    // NEWER one overtook while it was still in flight, not a finished predecessor.
    expect(write).toHaveBeenCalledWith(appId, "AA==", "png", 1);
    expect(write).toHaveBeenCalledWith(appId, "BB==", "png", 1);
  });

  it("an in-flight apply for one appId does not suppress a concurrent apply for a different appId", async () => {
    const slow = deferred<Art>();
    // appId X's rom (42) hangs; appId Y's rom (99) resolves fast.
    vi.mocked(backend.getSgdbArtworkBase64).mockImplementation((romId: number): Promise<Art> => {
      if (romId === 42) return slow.promise;
      return Promise.resolve({ base64: "YY==", no_api_key: false });
    });

    const xPromise = applyArtwork(42, 5000); // appId X, in flight
    await applyArtwork(99, 6000); // appId Y, different appId — completes

    const write = vi.mocked(SteamClient.Apps.SetCustomArtworkForApp);
    // Y wrote despite X being mid-flight (different appId → independent generation).
    expect(write).toHaveBeenCalledWith(6000, "YY==", "png", 1);

    // X is NOT superseded — Y bumped a different appId — so X still writes when it resolves.
    slow.resolve({ base64: "XX==", no_api_key: false });
    await expect(xPromise).resolves.toBe(4);
    expect(write).toHaveBeenCalledWith(5000, "XX==", "png", 1);
  });
});
