import { describe, it, expect, vi, beforeEach } from "vitest";
import { createOrUpdateRomMCollections, createOrUpdateCollections, clearPlatformCollection } from "./collections";

// Steam's collection identity is case-insensitive, so a collection whose
// displayName differs only by case from the one we're syncing is the SAME Steam
// collection. These tests pin that our create/find + cleanup match it
// case-insensitively — otherwise a colliding new create overwrites it and its
// games are lost (#1569). The getHostname mock (test-setup) resolves "test".

interface FakeDnd {
  RemoveApps: ReturnType<typeof vi.fn>;
  AddApps: ReturnType<typeof vi.fn>;
}
interface FakeCollection {
  id: string;
  displayName: string;
  allApps: unknown[];
  AsDragDropCollection: () => FakeDnd;
  Save: ReturnType<typeof vi.fn>;
  Delete: ReturnType<typeof vi.fn>;
  dnd: FakeDnd;
}

function fakeCollection(displayName: string): FakeCollection {
  const dnd: FakeDnd = { RemoveApps: vi.fn(), AddApps: vi.fn() };
  return {
    id: `id-${displayName}`,
    displayName,
    allApps: [],
    AsDragDropCollection: () => dnd,
    Save: vi.fn().mockResolvedValue(undefined),
    Delete: vi.fn().mockResolvedValue(undefined),
    dnd,
  };
}

describe("createOrUpdateRomMCollections — case-insensitive identity (#1569)", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("updates an existing case-variant collection instead of creating a duplicate", async () => {
    // Steam already holds "RomM: [7 Up] (test)"; the reporter now emits the
    // folded-first-seen casing "7 up". Must UPDATE the existing one, not create a
    // colliding "7 up" that Steam merges over the top of.
    const existing = fakeCollection("RomM: [7 Up] (test)");
    const NewUnsavedCollection = vi.fn();
    vi.stubGlobal("collectionStore", { userCollections: [existing], NewUnsavedCollection });

    await createOrUpdateRomMCollections({ "7 up": [1001, 1002] });

    expect(NewUnsavedCollection).not.toHaveBeenCalled();
    expect(existing.Save).toHaveBeenCalledTimes(1);
    expect(existing.dnd.AddApps).toHaveBeenCalledTimes(1);
  });

  it("creates a new collection when nothing matches even case-insensitively", async () => {
    // Non-vacuous complement: the find isn't matching everything — a genuinely
    // new name still creates.
    const created = fakeCollection("RomM: [brand new] (test)");
    const NewUnsavedCollection = vi.fn(() => created);
    vi.stubGlobal("collectionStore", { userCollections: [], NewUnsavedCollection });

    await createOrUpdateRomMCollections({ "brand new": [1] });

    expect(NewUnsavedCollection).toHaveBeenCalledTimes(1);
    expect(created.Save).toHaveBeenCalledTimes(1);
  });
});

describe("createOrUpdateCollections (platform) — case-insensitive identity (#1569)", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("updates an existing case-variant platform collection instead of creating a duplicate", async () => {
    const existing = fakeCollection("RomM: Game Boy (test)");
    const NewUnsavedCollection = vi.fn();
    vi.stubGlobal("collectionStore", { userCollections: [existing], NewUnsavedCollection });

    await createOrUpdateCollections({ "game boy": [1001] });

    expect(NewUnsavedCollection).not.toHaveBeenCalled();
    expect(existing.Save).toHaveBeenCalledTimes(1);
  });

  it("does not begin another collection mutation after cancellation", async () => {
    let settle!: () => void;
    const first = fakeCollection("RomM: First (test)");
    const pendingSave = new Promise<void>((resolve) => {
      settle = resolve;
    });
    first.Save.mockReturnValueOnce(pendingSave);
    const second = fakeCollection("RomM: Second (test)");
    vi.stubGlobal("collectionStore", { userCollections: [first, second], NewUnsavedCollection: vi.fn() });
    const controller = new AbortController();
    const applying = createOrUpdateCollections({ First: [1], Second: [2] }, undefined, controller.signal);
    await Promise.resolve();

    controller.abort();
    settle();
    await applying;

    expect(first.Save).toHaveBeenCalledTimes(1);
    expect(second.dnd.AddApps).not.toHaveBeenCalled();
    expect(second.Save).not.toHaveBeenCalled();
  });
});

describe("clearPlatformCollection — case-insensitive identity (#1569)", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("deletes a case-variant scoped collection", async () => {
    // We ask to clear "game boy"; the live collection is "RomM: Game Boy (test)".
    const scoped = fakeCollection("RomM: Game Boy (test)");
    vi.stubGlobal("collectionStore", { userCollections: [scoped] });

    await clearPlatformCollection("game boy");

    expect(scoped.Delete).toHaveBeenCalledTimes(1);
  });
});
