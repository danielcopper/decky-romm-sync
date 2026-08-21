import { describe, it, expect, vi, beforeEach } from "vitest";
import * as backend from "./backend";
import {
  getBiosStatusShared,
  getPlatformCoreInfoShared,
  getRomMetadataShared,
  _resetSharedReadsForTests,
} from "./sharedReads";
import type { BiosAnswer } from "./backend";
import type { CoreInfo, RomMetadata } from "../types";

/** A read a test can hold open across a second caller's arrival. */
function deferred<T>(): { promise: Promise<T>; resolve: (value: T) => void; reject: (e: unknown) => void } {
  let resolve!: (value: T) => void;
  let reject!: (e: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

const romMetadata = (summary: string): RomMetadata => ({
  summary,
  genres: [],
  companies: [],
  first_release_date: null,
  average_rating: null,
  game_modes: [],
  player_count: "",
  cached_at: 0,
});

const coreInfo = (label: string): CoreInfo => ({
  active_core: "snes9x.so",
  active_core_label: label,
  platform_core_label: null,
  has_game_override: false,
  emulator_data_available: true,
  emulators: [],
});

describe("sharedReads (#1758)", () => {
  beforeEach(() => {
    vi.resetAllMocks();
    _resetSharedReadsForTests();
  });

  it("collapses two overlapping callers into ONE backend call", async () => {
    const open = deferred<CoreInfo>();
    vi.mocked(backend.getPlatformCoreInfo).mockReturnValue(open.promise);

    const panel = getPlatformCoreInfoShared(42);
    const playRow = getPlatformCoreInfoShared(42);
    open.resolve(coreInfo("Snes9x"));
    await Promise.all([panel, playRow]);

    expect(vi.mocked(backend.getPlatformCoreInfo)).toHaveBeenCalledExactlyOnceWith(42);
  });

  // The two answers below differ, so a second caller that opened its own request
  // would be visible as a different label rather than only as a second call.
  it("hands both callers the same answer", async () => {
    const open = deferred<CoreInfo>();
    vi.mocked(backend.getPlatformCoreInfo).mockReturnValueOnce(open.promise);
    vi.mocked(backend.getPlatformCoreInfo).mockResolvedValue(coreInfo("Genesis Plus GX"));

    const panel = getPlatformCoreInfoShared(42);
    const playRow = getPlatformCoreInfoShared(42);
    open.resolve(coreInfo("Snes9x"));

    expect((await panel).active_core_label).toBe("Snes9x");
    expect((await playRow).active_core_label).toBe("Snes9x");
  });

  // Sharing collapses concurrent callers; it must not turn into a cache that
  // outlives the request and answers a later read with a stale payload.
  it("releases the request once it settles, so a later call reads again", async () => {
    vi.mocked(backend.getPlatformCoreInfo).mockResolvedValueOnce(coreInfo("Snes9x"));
    vi.mocked(backend.getPlatformCoreInfo).mockResolvedValueOnce(coreInfo("Genesis Plus GX"));

    const first = await getPlatformCoreInfoShared(42);
    const second = await getPlatformCoreInfoShared(42);

    expect(vi.mocked(backend.getPlatformCoreInfo)).toHaveBeenCalledTimes(2);
    expect(first.active_core_label).toBe("Snes9x");
    expect(second.active_core_label).toBe("Genesis Plus GX");
  });

  it("releases a REJECTED request too, so a later call is not stuck on the failure", async () => {
    vi.mocked(backend.getPlatformCoreInfo).mockRejectedValueOnce(new Error("offline"));
    vi.mocked(backend.getPlatformCoreInfo).mockResolvedValueOnce(coreInfo("Snes9x"));

    await expect(getPlatformCoreInfoShared(42)).rejects.toThrow("offline");
    await expect(getPlatformCoreInfoShared(42)).resolves.toMatchObject({ active_core_label: "Snes9x" });
    expect(vi.mocked(backend.getPlatformCoreInfo)).toHaveBeenCalledTimes(2);
  });

  // Same shape as the success case: only a caller that JOINED the failed request
  // rejects, where one that opened its own would have resolved.
  it("gives every caller of a rejected request the rejection", async () => {
    const open = deferred<CoreInfo>();
    vi.mocked(backend.getPlatformCoreInfo).mockReturnValueOnce(open.promise);
    vi.mocked(backend.getPlatformCoreInfo).mockResolvedValue(coreInfo("Snes9x"));

    const panel = getPlatformCoreInfoShared(42);
    const playRow = getPlatformCoreInfoShared(42);
    open.reject(new Error("offline"));

    await expect(panel).rejects.toThrow("offline");
    await expect(playRow).rejects.toThrow("offline");
  });

  it("shares per ROM — a read for another ROM is its own request", async () => {
    const open = deferred<CoreInfo>();
    vi.mocked(backend.getPlatformCoreInfo).mockReturnValue(open.promise);

    const first = getPlatformCoreInfoShared(42);
    const second = getPlatformCoreInfoShared(43);
    open.resolve(coreInfo("Snes9x"));
    await Promise.all([first, second]);

    expect(vi.mocked(backend.getPlatformCoreInfo)).toHaveBeenCalledTimes(2);
    expect(vi.mocked(backend.getPlatformCoreInfo)).toHaveBeenCalledWith(42);
    expect(vi.mocked(backend.getPlatformCoreInfo)).toHaveBeenCalledWith(43);
  });

  // Issued together, not one after the other: sharing is keyed on the argument,
  // so two DIFFERENT reads of the same ROM overlapping is exactly the case where
  // one key could answer for the other.
  it("shares per read — metadata and core info do not answer for each other", async () => {
    const metadata = romMetadata("Test ROM");
    vi.mocked(backend.getRomMetadata).mockResolvedValue(metadata);
    vi.mocked(backend.getPlatformCoreInfo).mockResolvedValue(coreInfo("Snes9x"));

    const [meta, core] = await Promise.all([getRomMetadataShared(42), getPlatformCoreInfoShared(42)]);

    expect(meta).toBe(metadata);
    expect(core).toMatchObject({ active_core_label: "Snes9x" });
    expect(vi.mocked(backend.getRomMetadata)).toHaveBeenCalledExactlyOnceWith(42);
    expect(vi.mocked(backend.getPlatformCoreInfo)).toHaveBeenCalledExactlyOnceWith(42);
  });

  it("collapses two overlapping BIOS callers into ONE backend call", async () => {
    const open = deferred<BiosAnswer>();
    vi.mocked(backend.getBiosStatus).mockReturnValue(open.promise);

    const panel = getBiosStatusShared(42);
    const playRow = getBiosStatusShared(42);
    const answer: BiosAnswer = { bios_level: "missing" };
    open.resolve(answer);

    expect(await panel).toBe(answer);
    expect(await playRow).toBe(answer);
    expect(vi.mocked(backend.getBiosStatus)).toHaveBeenCalledExactlyOnceWith(42);
  });

  it("collapses two overlapping metadata callers into ONE backend call", async () => {
    const open = deferred<RomMetadata>();
    vi.mocked(backend.getRomMetadata).mockReturnValue(open.promise);

    const panel = getRomMetadataShared(42);
    const playRow = getRomMetadataShared(42);
    const answer = romMetadata("Test ROM");
    open.resolve(answer);

    expect(await panel).toBe(answer);
    expect(await playRow).toBe(answer);
    expect(vi.mocked(backend.getRomMetadata)).toHaveBeenCalledExactlyOnceWith(42);
  });
});
