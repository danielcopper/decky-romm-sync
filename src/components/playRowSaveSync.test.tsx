// The play row's save-sync fan-out, end to end: RomMPlaySection reads this
// ROM's save status once and broadcasts it, and the real CustombPlayButton it
// wraps flips off that broadcast. Both components are REAL here — the halves are
// tested separately in their own files, and this is the only place that proves
// the producer's event actually satisfies the consumer's guards. The button used
// to trigger a second, backend-side read of its own for the same broadcast
// (#1758); the absence assertion is what keeps it from coming back.

import { describe, it, expect, beforeEach, vi } from "vitest";
import { act, render } from "@testing-library/react";
import { RomMPlaySection } from "./RomMPlaySection";
import * as backend from "../api/backend";
import * as cachedStore from "../utils/cachedGameDetailStore";
import { _resetSharedReadsForTests } from "../api/sharedReads";
import { setRommConnectionState, setVersionError } from "../utils/connectionState";
import { registerConnectionHeartbeat } from "../utils/connectionHeartbeat";
import { stubAppStore } from "../test-utils/steamStubs";
import { useVersionError } from "./VersionErrorCard";
import { useMigrationStatus } from "./MigrationBlockedPage";
import type { SaveStatus, SyncConflict } from "../types";

vi.mock("./VersionErrorCard", () => ({ useVersionError: vi.fn(() => null) }));
vi.mock("./MigrationBlockedPage", () => ({ useMigrationStatus: vi.fn(() => ({ pending: false })) }));
// The heartbeat owns a real setInterval; the artwork apply and the playtime
// overview write both reach into Steam. None of the three is on the path under
// test, so they are stubbed inert rather than driven.
vi.mock("../utils/connectionHeartbeat", () => ({ registerConnectionHeartbeat: vi.fn(() => () => {}) }));
vi.mock("../utils/artwork", () => ({
  applyArtwork: vi.fn(() => Promise.resolve()),
  cancelArtworkApply: vi.fn(() => Promise.resolve()),
}));
vi.mock("../patches/metadataPatches", () => ({ updatePlaytimeDisplay: vi.fn() }));
// The store's BIOS / core / achievement background refreshes are not on the path
// under test and each is a long chain of its own; inert here so the only reads
// this file observes are the save-status ones it is about.
vi.mock("../utils/sectionRefresh", () => ({
  refreshBiosInBackground: vi.fn(),
  refreshCoreInfoInBackground: vi.fn(),
  refreshAchievementsInBackground: vi.fn(),
}));
// getCachedGameDetail is re-exported through backend.ts but lives in utils —
// mock the store so the section and the button route through the same vi.fn.
vi.mock("../utils/cachedGameDetailStore", () => ({
  getCachedGameDetail: vi.fn(),
  invalidateCachedGameDetail: vi.fn(),
}));

/** Drain the mount's chained reads so nothing lands after the test ends. The
 *  whole row mounts here, and its longest chain is several awaits deep. */
const flushAsync = () =>
  act(async () => {
    for (let round = 0; round < 4; round++) {
      for (let tick = 0; tick < 12; tick++) await Promise.resolve();
      await new Promise((resolve) => setTimeout(resolve, 0));
    }
  });

const conflict: SyncConflict = {
  type: "sync_conflict",
  rom_id: 88,
  filename: "save.srm",
  server_save_id: 7,
  server_updated_at: "2026-01-01T00:00:00Z",
  server_size: 1,
  local_path: null,
  local_hash: null,
  local_mtime: null,
  local_size: null,
  created_at: "2026-01-01T00:00:00Z",
};

const saveStatus = (overrides: Partial<SaveStatus> = {}): SaveStatus => ({
  rom_id: 88,
  files: [],
  playtime: {
    total_seconds: 0,
    session_count: 0,
    last_session_start: null,
    last_session_duration_sec: null,
    last_played: null,
  },
  device_id: "device",
  last_sync_check_at: null,
  ...overrides,
});

// A page-open state the button settles into "Play" on: installed, save sync on,
// and nothing in the CACHED status to object to.
const cachedDetail = {
  found: true as const,
  rom_id: 88,
  rom_name: "Test ROM",
  platform_slug: "snes",
  installed: true,
  save_sync_enabled: true,
};

describe("play row save-sync fan-out (#1758)", () => {
  let appId = 9000;

  beforeEach(() => {
    // clearAllMocks, not resetAllMocks: the whole row renders here, so every
    // callable it touches on the way to a first paint must keep the
    // undefined-resolving default the @decky/api stub gives it. Resetting them
    // hands the row a bare `undefined` where it awaits a promise, and the tree
    // never renders at all.
    vi.clearAllMocks();
    _resetSharedReadsForTests();
    appId += 1;
    stubAppStore({ [appId]: { appid: appId, display_name: "Test ROM", strDisplayName: "Test ROM" } });
    setRommConnectionState("connected");
    setVersionError(null);
    vi.mocked(useVersionError).mockReturnValue(null);
    vi.mocked(useMigrationStatus).mockReturnValue({ pending: false });
    vi.mocked(registerConnectionHeartbeat).mockReturnValue(() => {});
    vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue(cachedDetail);
    vi.mocked(backend.testConnection).mockResolvedValue({ success: true, message: "ok" });
  });

  it("flips the play button to Resolve Conflict off the section's broadcast, with no read of its own", async () => {
    vi.mocked(backend.getSaveStatus).mockResolvedValue(saveStatus({ conflicts: [conflict] }));

    const { container } = render(<RomMPlaySection appId={appId} />);
    await flushAsync();

    // The live status the section read carried a conflict the cached one did not.
    expect(container.textContent).toContain("Resolve Conflict");
    // One read for the whole row: the store's. The button contributes none —
    // neither the fire-and-forget backend refresh it used to fire, nor a direct
    // status read.
    expect(vi.mocked(backend.refreshSaveStatus)).not.toHaveBeenCalled();
    expect(vi.mocked(backend.getSaveStatus)).toHaveBeenCalledExactlyOnceWith(88);
  });

  it("leaves the button on Play when the live status carries no conflict", async () => {
    vi.mocked(backend.getSaveStatus).mockResolvedValue(saveStatus({ conflicts: [] }));

    const { container } = render(<RomMPlaySection appId={appId} />);
    await flushAsync();

    expect(vi.mocked(backend.getSaveStatus)).toHaveBeenCalledExactlyOnceWith(88);
    expect(vi.mocked(backend.refreshSaveStatus)).not.toHaveBeenCalled();
    expect(container.textContent).toContain("Play");
    expect(container.textContent).not.toContain("Resolve Conflict");
  });

  it("repairs the row off a reconnect broadcast when the page opened without a server half", async () => {
    // Opened while RomM was unreachable: the status carries the local half only,
    // so nothing on the page knows about the conflict the server holds.
    vi.mocked(backend.testConnection).mockResolvedValue({ success: false, message: "" });
    vi.mocked(backend.getSaveStatus)
      .mockResolvedValueOnce(saveStatus({ server_query_failed: true, conflicts: [] }))
      .mockResolvedValueOnce(saveStatus({ conflicts: [conflict] }));

    const { container } = render(<RomMPlaySection appId={appId} />);
    await flushAsync();
    expect(container.textContent).not.toContain("Resolve Conflict");
    expect(vi.mocked(backend.getSaveStatus)).toHaveBeenCalledExactlyOnceWith(88);

    // The server comes back WITHOUT the page being closed — the outage story
    // this row is about (#1758).
    act(() => {
      setRommConnectionState("connected");
    });
    await flushAsync();

    // One fresh read for the whole row, and its answer reaches the button
    // through the same broadcast the mount read uses — the button still
    // contributes no read of its own.
    expect(vi.mocked(backend.getSaveStatus)).toHaveBeenCalledTimes(2);
    expect(vi.mocked(backend.refreshSaveStatus)).not.toHaveBeenCalled();
    expect(container.textContent).toContain("Resolve Conflict");
  });
});
