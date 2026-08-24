import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { render, fireEvent, act, waitFor } from "@testing-library/react";
import { createElement, type ComponentProps, type ReactElement } from "react";
import { SavesTab } from "./SavesTab";
import * as backend from "../api/backend";
import { showModal } from "@decky/ui";
import * as connectionState from "../utils/connectionState";
import { setRommConnectionState } from "../utils/connectionState";
import type { SaveStatus, SaveSlotSummary, SaveFileStatus, SwitchSlotResponse, LastKnownSlots } from "../types";
// Type-only — vi.mock("./saves/SlotPanel", ...) below replaces the runtime
// implementation, but the prop interface comes from the real component so
// captured-prop assertions stay in sync as SlotPanel evolves.
import type { SlotPanel } from "./saves/SlotPanel";
import { installDomEventListenerSpy, uninstallDomEventListenerSpy } from "../test-utils/dom-event-listener-spy";

// showModal from the global @decky/ui mock receives the <NewSlotModal> element.
// Tests pull `props.onSubmit` off the captured element to drive the new-slot flow.
interface NewSlotModalProps {
  onSubmit?: (name: string) => void | Promise<void>;
}

// The real in-memory connection store is driven directly (setRommConnectionState)
// so the offline banner's live subscription is exercised end-to-end (#1345).

// Stub NewSlotModal — its own tests cover the text-field + trim behavior.
// SavesTab only cares that it gets rendered with an onSubmit, which we capture
// via showModal.mock.calls[N][0].props.onSubmit.
vi.mock("./saves/NewSlotModal", () => ({
  NewSlotModal: (_p: NewSlotModalProps) => createElement("div", { "data-testid": "new-slot-modal" }),
}));

// Stub SlotPanel — its own tests cover expand/collapse/activate/delete.
// We capture props per render so tests can assert sort order, active flag,
// saveStatus pass-through, and trigger the version-restored + slot-deleted
// callbacks. The captured-props type is derived from the real SlotPanel
// component (via ComponentProps + type-only import) so any new prop on the
// real component widens this type automatically — assertions missing the new
// field surface as type-narrowing issues under strict TS.
type CapturedSlotPanelProps = ComponentProps<typeof SlotPanel>;
let capturedSlotPanelProps: CapturedSlotPanelProps[] = [];
vi.mock("./saves/SlotPanel", () => ({
  SlotPanel: (p: CapturedSlotPanelProps) => {
    capturedSlotPanelProps.push(p);
    return createElement("div", {
      "data-testid": `slot-panel-${p.slot.slot || "legacy"}`,
      "data-active": String(p.isActive),
    });
  },
}));

// Stub renderSaveFileRow — keeps the legacy-files branch trivial to assert
// without dragging in the full DialogButton render tree. Keyed by filename like
// the real one: these are rendered as a list, and an unkeyed stub warns.
vi.mock("./saves/SaveFileRow", () => ({
  renderSaveFileRow: (f: SaveFileStatus) =>
    createElement("div", { key: f.filename, "data-testid": `save-file-row-${f.filename}` }, f.filename),
}));

function makeSlot(overrides: Partial<SaveSlotSummary> = {}): SaveSlotSummary {
  return {
    slot: "default",
    source: "local",
    count: 0,
    latest_updated_at: null,
    ...overrides,
  };
}

function makeSaveStatus(overrides: Partial<SaveStatus> = {}): SaveStatus {
  return {
    rom_id: 1,
    files: [],
    playtime: {
      total_seconds: 0,
      session_count: 0,
      last_session_start: null,
      last_session_duration_sec: null,
      last_played: null,
    },
    device_id: "dev",
    last_sync_check_at: null,
    ...overrides,
  };
}

function makeSaveFile(overrides: Partial<SaveFileStatus> = {}): SaveFileStatus {
  return {
    filename: "a.srm",
    local_path: "/data/a.srm",
    local_hash: "h",
    local_mtime: "2025-06-15T10:00:00Z",
    local_size: 100,
    server_save_id: 1,
    server_file_name: null,
    server_emulator: null,
    server_updated_at: null,
    server_size: null,
    last_sync_at: null,
    status: "synced",
    ...overrides,
  };
}

function defaultProps(
  overrides: Partial<React.ComponentProps<typeof SavesTab>> = {},
): React.ComponentProps<typeof SavesTab> {
  return {
    appId: 100,
    romId: 1,
    saveStatus: null,
    conflicts: [],
    activeSlot: "default",
    activeSlotKnown: true,
    availableSlots: [],
    lastKnownSlots: null,
    slotsLoading: false,
    onSlotSwitched: vi.fn(),
    ...overrides,
  };
}

// Helper: pull the onSubmit prop off the NewSlotModal element passed to
// showModal at call index `idx`, for the named-arg flow we own here.
function newSlotModalSubmit(idx = 0): ((name: string) => Promise<void>) | undefined {
  const calls = vi.mocked(showModal).mock.calls;
  const el = calls[idx]?.[0] as ReactElement<NewSlotModalProps> | undefined;
  return el?.props.onSubmit as ((name: string) => Promise<void>) | undefined;
}

// Settles the mount-time stranded-version probe (getVersionList → checkLocalDrift).
// A test that returns before it resolves leaves its setStrandedVersion to land
// unattributed, outside act.
const flushAsync = () =>
  act(async () => {
    await Promise.resolve();
    await Promise.resolve();
    await Promise.resolve();
  });

describe("SavesTab", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    capturedSlotPanelProps = [];
    setRommConnectionState("connected");
    // Stranded-version banner probes (#1298) — default to "no other versions" so
    // the banner is absent for the unrelated slot/legacy tests.
    vi.mocked(backend.getVersionList).mockResolvedValue({ multi_version: false, bound_vanished: false });
    vi.mocked(backend.checkLocalDrift).mockResolvedValue({ drifted: false, rom_id: 0 });
    installDomEventListenerSpy();
  });

  afterEach(() => {
    uninstallDomEventListenerSpy();
  });

  describe("loading state", () => {
    it("renders the connecting spinner when slotsLoading is true", async () => {
      const { container, queryByTestId } = render(<SavesTab {...defaultProps({ slotsLoading: true })} />);
      expect(container.textContent).toContain("Connecting to RomM…");
      expect(container.querySelector(".romm-throbber")).not.toBeNull();
      expect(queryByTestId("slot-panel-default")).toBeNull();
      await flushAsync();
    });

    it("still renders the offline banner alongside the connecting spinner", async () => {
      setRommConnectionState("offline");
      const { container } = render(<SavesTab {...defaultProps({ slotsLoading: true })} />);
      expect(container.textContent).toContain("Connecting to RomM…");
      expect(container.textContent).toContain("RomM is offline");
      await flushAsync();
    });
  });

  describe("offline banner", () => {
    it("does not render when connection state is 'connected'", async () => {
      const { container } = render(<SavesTab {...defaultProps()} />);
      expect(container.textContent).not.toContain("RomM is offline");
      await flushAsync();
    });

    it("renders when the store is 'offline' at mount", async () => {
      setRommConnectionState("offline");
      const { container } = render(<SavesTab {...defaultProps()} />);
      expect(container.textContent).toContain("RomM is offline");
      await flushAsync();
    });

    it("appears live when the store flips to offline (no remount)", async () => {
      const { container } = render(<SavesTab {...defaultProps()} />);
      await flushAsync();
      expect(container.textContent).not.toContain("RomM is offline");
      act(() => {
        setRommConnectionState("offline");
      });
      expect(container.textContent).toContain("RomM is offline");
    });

    it("clears live when the store flips back to connected (no remount)", async () => {
      setRommConnectionState("offline");
      const { container } = render(<SavesTab {...defaultProps()} />);
      await flushAsync();
      expect(container.textContent).toContain("RomM is offline");
      act(() => {
        setRommConnectionState("connected");
      });
      expect(container.textContent).not.toContain("RomM is offline");
    });

    it("unsubscribes from the connection store on unmount", () => {
      const unsub = vi.fn();
      const spy = vi.spyOn(connectionState, "onRommConnectionChange").mockReturnValue(unsub);
      const { unmount } = render(<SavesTab {...defaultProps()} />);
      expect(spy).toHaveBeenCalledTimes(1);
      unmount();
      expect(unsub).toHaveBeenCalledTimes(1);
    });

    it("forwards isOffline down to SlotPanel children", async () => {
      setRommConnectionState("offline");
      render(
        <SavesTab
          {...defaultProps({
            availableSlots: [makeSlot()],
          })}
        />,
      );
      expect(capturedSlotPanelProps[0]?.isOffline).toBe(true);
      await flushAsync();
    });
  });

  describe("legacy-mode warning + files section", () => {
    it("renders the legacy warning when activeSlot is null", async () => {
      const { container } = render(<SavesTab {...defaultProps({ activeSlot: null })} />);
      expect(container.textContent).toContain("This game uses legacy mode");
      await flushAsync();
    });

    it("does NOT render the legacy warning when activeSlot is a real slot", async () => {
      const { container } = render(<SavesTab {...defaultProps()} />);
      expect(container.textContent).not.toContain("This game uses legacy mode");
      await flushAsync();
    });

    it("renders legacy save-file rows when activeSlot is null and saveStatus has files", async () => {
      const status = makeSaveStatus({
        files: [makeSaveFile({ filename: "a.srm" }), makeSaveFile({ filename: "b.srm" })],
      });
      const { queryByTestId } = render(<SavesTab {...defaultProps({ activeSlot: null, saveStatus: status })} />);
      expect(queryByTestId("save-file-row-a.srm")).not.toBeNull();
      expect(queryByTestId("save-file-row-b.srm")).not.toBeNull();
      await flushAsync();
    });

    it("renders the 'No save files tracked yet' empty state when activeSlot is null and no files", async () => {
      const { container } = render(
        <SavesTab
          {...defaultProps({
            activeSlot: null,
            saveStatus: makeSaveStatus({ files: [] }),
          })}
        />,
      );
      expect(container.textContent).toContain("No save files tracked yet");
      await flushAsync();
    });

    it("renders the empty state when activeSlot is null and saveStatus is null", async () => {
      const { container } = render(<SavesTab {...defaultProps({ activeSlot: null, saveStatus: null })} />);
      expect(container.textContent).toContain("No save files tracked yet");
      await flushAsync();
    });

    it("hides the legacy '' slot panel when activeSlot is null", async () => {
      // availableSlots may carry a legacy "" entry — SavesTab filters it out
      // when activeSlot is already null (the legacy-files section above
      // replaces it).
      const { queryByTestId } = render(
        <SavesTab
          {...defaultProps({
            activeSlot: null,
            availableSlots: [makeSlot({ slot: "" }), makeSlot({ slot: "alpha" })],
          })}
        />,
      );
      expect(queryByTestId("slot-panel-legacy")).toBeNull();
      expect(queryByTestId("slot-panel-alpha")).not.toBeNull();
      await flushAsync();
    });
  });

  describe("slot sorting + active-slot synthesis", () => {
    it("sorts active slot first, then alphabetically", async () => {
      render(
        <SavesTab
          {...defaultProps({
            activeSlot: "b",
            availableSlots: [makeSlot({ slot: "c" }), makeSlot({ slot: "a" }), makeSlot({ slot: "b" })],
          })}
        />,
      );
      const order = capturedSlotPanelProps.map((p) => p.slot.slot);
      expect(order).toEqual(["b", "a", "c"]);
      await flushAsync();
    });

    it("sorts the legacy '' bucket last, below every named slot (#1478)", async () => {
      render(
        <SavesTab
          {...defaultProps({
            activeSlot: "b",
            availableSlots: [
              makeSlot({ slot: "c" }),
              makeSlot({ slot: "" }),
              makeSlot({ slot: "a" }),
              makeSlot({ slot: "b" }),
            ],
          })}
        />,
      );
      const order = capturedSlotPanelProps.map((p) => p.slot.slot);
      // active first, then named alphabetically, legacy "" demoted to the end.
      expect(order).toEqual(["b", "a", "c", ""]);
      await flushAsync();
    });

    it("marks the active slot with isActive=true and forwards saveStatus/conflicts only to it", async () => {
      const status = makeSaveStatus();
      const conflicts = [
        {
          type: "sync_conflict" as const,
          rom_id: 1,
          filename: "a.srm",
          server_save_id: 1,
          server_updated_at: "",
          server_size: null,
          local_path: null,
          local_hash: null,
          local_mtime: null,
          local_size: null,
          created_at: "",
        },
      ];
      render(
        <SavesTab
          {...defaultProps({
            activeSlot: "a",
            saveStatus: status,
            conflicts,
            availableSlots: [makeSlot({ slot: "a" }), makeSlot({ slot: "b" })],
          })}
        />,
      );
      const active = capturedSlotPanelProps.find((p) => p.slot.slot === "a");
      const inactive = capturedSlotPanelProps.find((p) => p.slot.slot === "b");
      expect(active?.isActive).toBe(true);
      expect(active?.saveStatus).toBe(status);
      expect(active?.conflicts).toBe(conflicts);
      expect(active?.defaultExpanded).toBe(true);
      expect(active?.romId).toBe(1);
      expect(inactive?.isActive).toBe(false);
      expect(inactive?.saveStatus).toBeNull();
      expect(inactive?.conflicts).toEqual([]);
      expect(inactive?.defaultExpanded).toBe(false);
      expect(inactive?.romId).toBe(1);
      await flushAsync();
    });

    it("synthesizes a placeholder for an active slot missing from availableSlots", async () => {
      render(
        <SavesTab
          {...defaultProps({
            activeSlot: "ghost",
            activeSlotKnown: true,
            availableSlots: [makeSlot({ slot: "alpha" })],
          })}
        />,
      );
      const order = capturedSlotPanelProps.map((p) => p.slot.slot);
      expect(order[0]).toBe("ghost");
      const ghost = capturedSlotPanelProps[0];
      expect(ghost?.slot.source).toBe("local");
      expect(ghost?.slot.count).toBe(0);
      expect(ghost?.slot.latest_updated_at).toBeNull();
      expect(ghost?.isActive).toBe(true);
      await flushAsync();
    });

    it("does NOT synthesize a placeholder when activeSlot is null", async () => {
      render(
        <SavesTab
          {...defaultProps({
            activeSlot: null,
            availableSlots: [makeSlot({ slot: "alpha" })],
          })}
        />,
      );
      const order = capturedSlotPanelProps.map((p) => p.slot.slot);
      expect(order).toEqual(["alpha"]);
      await flushAsync();
    });
  });

  describe("unanswered slot list (#1747)", () => {
    it("names no slot while no slot answer has landed, even with the server unreachable", async () => {
      setRommConnectionState("offline");
      const { queryByTestId, container } = render(
        <SavesTab {...defaultProps({ activeSlot: "default", activeSlotKnown: false, availableSlots: [] })} />,
      );
      expect(capturedSlotPanelProps).toEqual([]);
      expect(queryByTestId("slot-panel-default")).toBeNull();
      // The offline banner is the one and only word on reachability here.
      expect(container.textContent).toContain("RomM is offline");
      await flushAsync();
    });

    it("keeps the locally tracked save files on screen while no slot answer has landed", async () => {
      const { queryByTestId } = render(
        <SavesTab
          {...defaultProps({
            activeSlotKnown: false,
            saveStatus: makeSaveStatus({ files: [makeSaveFile({ filename: "zelda.srm" })] }),
          })}
        />,
      );
      expect(queryByTestId("save-file-row-zelda.srm")).not.toBeNull();
      expect(capturedSlotPanelProps).toEqual([]);
      await flushAsync();
    });

    it("shows the tracked-files empty state without claiming legacy mode", async () => {
      const { container } = render(<SavesTab {...defaultProps({ activeSlotKnown: false, saveStatus: null })} />);
      expect(container.textContent).toContain("No save files tracked yet");
      expect(container.textContent).not.toContain("legacy mode");
      await flushAsync();
    });

    it("files go back under the active slot panel once a slot answer has landed", async () => {
      const status = makeSaveStatus({ files: [makeSaveFile({ filename: "zelda.srm" })] });
      const { queryByTestId } = render(
        <SavesTab
          {...defaultProps({
            activeSlot: "main",
            activeSlotKnown: true,
            availableSlots: [makeSlot({ slot: "main" })],
            saveStatus: status,
          })}
        />,
      );
      // Under the panel, and only there — a bare row alongside it would show
      // the same file twice.
      expect(capturedSlotPanelProps[0]?.saveStatus).toBe(status);
      expect(queryByTestId("save-file-row-zelda.srm")).toBeNull();
      await flushAsync();
    });
  });

  describe("last-known slots (#1755)", () => {
    const lastKnown = (overrides: Partial<LastKnownSlots> = {}): LastKnownSlots => ({
      slots: [makeSlot({ slot: "main", source: "server", count: 3, latest_updated_at: "2026-04-17T10:00:00" })],
      activeSlot: "main",
      ...overrides,
    });

    it("shows the last contact's slots, marked as from then, while nothing live has landed", async () => {
      setRommConnectionState("offline");
      const { getByTestId, container } = render(
        <SavesTab {...defaultProps({ activeSlotKnown: false, availableSlots: [], lastKnownSlots: lastKnown() })} />,
      );
      const block = getByTestId("last-known-slots");
      expect(block.textContent).toContain("main");
      expect(block.textContent).toContain("3 saves");
      expect(block.textContent).toContain("counts and times are from that answer, not from now");
      // The active slot of the snapshot is marked as such, and only there.
      expect(getByTestId("last-known-slot-main").textContent).toContain("active");
      // Still no live slot panel and no second word on reachability.
      expect(capturedSlotPanelProps).toEqual([]);
      expect(container.textContent).toContain("RomM is offline");
      expect(container.textContent).not.toContain("Server unreachable");
      await flushAsync();
    });

    it("marks no slot active when the snapshot's active slot is not among them", async () => {
      const { getByTestId } = render(
        <SavesTab
          {...defaultProps({
            activeSlotKnown: false,
            lastKnownSlots: lastKnown({
              slots: [makeSlot({ slot: "alpha" }), makeSlot({ slot: "beta" })],
              activeSlot: "gone",
            }),
          })}
        />,
      );
      expect(getByTestId("last-known-slot-alpha").textContent).not.toContain("active");
      expect(getByTestId("last-known-slot-beta").textContent).not.toContain("active");
      await flushAsync();
    });

    it("renders nothing pressable in the stale list", async () => {
      const { getByTestId } = render(
        <SavesTab {...defaultProps({ activeSlotKnown: false, lastKnownSlots: lastKnown() })} />,
      );
      expect(getByTestId("last-known-slots").querySelectorAll("button")).toHaveLength(0);
      await flushAsync();
    });

    it("keeps the '+ New Slot' affordance exactly as it is without a snapshot", async () => {
      const { getByText } = render(
        <SavesTab {...defaultProps({ activeSlotKnown: false, lastKnownSlots: lastKnown() })} />,
      );
      fireEvent.click(getByText("+ New Slot"));
      expect(vi.mocked(showModal)).toHaveBeenCalledTimes(1);
      await flushAsync();
    });

    it("lists the tracked save files alongside the snapshot, still unfiled (#1747)", async () => {
      const { queryByTestId } = render(
        <SavesTab
          {...defaultProps({
            activeSlotKnown: false,
            lastKnownSlots: lastKnown(),
            saveStatus: makeSaveStatus({ files: [makeSaveFile({ filename: "zelda.srm" })] }),
          })}
        />,
      );
      expect(queryByTestId("save-file-row-zelda.srm")).not.toBeNull();
      expect(queryByTestId("last-known-slots")).not.toBeNull();
      expect(capturedSlotPanelProps).toEqual([]);
      await flushAsync();
    });

    it("falls back to #1747's slot-less view when there is no snapshot", async () => {
      setRommConnectionState("offline");
      const { queryByTestId, container } = render(
        <SavesTab {...defaultProps({ activeSlotKnown: false, lastKnownSlots: null })} />,
      );
      expect(queryByTestId("last-known-slots")).toBeNull();
      expect(container.textContent).toContain("No save files tracked yet");
      expect(capturedSlotPanelProps).toEqual([]);
      await flushAsync();
    });

    it("shows the live slots and no snapshot once a slot answer has landed", async () => {
      const { queryByTestId } = render(
        <SavesTab
          {...defaultProps({
            activeSlot: "main",
            activeSlotKnown: true,
            availableSlots: [makeSlot({ slot: "main" })],
            lastKnownSlots: lastKnown(),
          })}
        />,
      );
      expect(queryByTestId("last-known-slots")).toBeNull();
      expect(capturedSlotPanelProps.map((p) => p.slot.slot)).toEqual(["main"]);
      expect(capturedSlotPanelProps[0]?.isActive).toBe(true);
      await flushAsync();
    });

    it("shows no snapshot while the slot fetch is in flight", async () => {
      const { queryByTestId, container } = render(
        <SavesTab {...defaultProps({ activeSlotKnown: false, lastKnownSlots: lastKnown(), slotsLoading: true })} />,
      );
      expect(queryByTestId("last-known-slots")).toBeNull();
      expect(container.textContent).toContain("Connecting to RomM…");
      await flushAsync();
    });
  });

  describe("new-slot button", () => {
    it("renders the '+ New Slot' button", async () => {
      const { getByText } = render(<SavesTab {...defaultProps()} />);
      expect(getByText("+ New Slot")).not.toBeNull();
      await flushAsync();
    });

    it("opens the NewSlotModal when clicked", async () => {
      const { getByText } = render(<SavesTab {...defaultProps()} />);
      fireEvent.click(getByText("+ New Slot"));
      expect(vi.mocked(showModal)).toHaveBeenCalledTimes(1);
      await flushAsync();
    });

    it("is disabled while the server is unreachable", async () => {
      // Creating a slot is a server write; offline it can only fail, and the
      // banner above already says slot switching is off.
      setRommConnectionState("offline");
      const { getByText } = render(<SavesTab {...defaultProps()} />);
      const button = getByText("+ New Slot") as HTMLButtonElement;
      expect(button.disabled).toBe(true);
      fireEvent.click(button);
      expect(vi.mocked(showModal)).not.toHaveBeenCalled();
      await flushAsync();
    });
  });

  describe("new-slot submit — empty name", () => {
    it("is a no-op: no legacy-mode confirm modal and no switchSlot call (#1478)", async () => {
      // Switching into the slot-less legacy bucket is retired (#1276): an empty
      // slot name is ignored — it must not open a "Use Legacy Mode?" confirm or
      // call switchSlot("").
      const onSlotSwitched = vi.fn();
      const { getByText } = render(<SavesTab {...defaultProps({ onSlotSwitched })} />);
      fireEvent.click(getByText("+ New Slot"));
      await act(async () => {
        await newSlotModalSubmit()?.("");
      });
      // Only the NewSlotModal opened — no second (legacy-confirm) modal.
      expect(vi.mocked(showModal).mock.calls).toHaveLength(1);
      expect(vi.mocked(backend.switchSlot)).not.toHaveBeenCalled();
      expect(onSlotSwitched).not.toHaveBeenCalled();
    });
  });

  describe("new-slot submit — named slot", () => {
    it("calls switchSlot(name) and onSlotSwitched on success", async () => {
      const newStatus = makeSaveStatus();
      vi.mocked(backend.switchSlot).mockResolvedValue({
        success: true,
        save_status: newStatus,
      });
      const onSlotSwitched = vi.fn();
      const { getByText } = render(<SavesTab {...defaultProps({ onSlotSwitched })} />);
      fireEvent.click(getByText("+ New Slot"));
      await act(async () => {
        await newSlotModalSubmit()?.("newslot");
      });
      expect(vi.mocked(backend.switchSlot)).toHaveBeenCalledWith(1, "newslot");
      expect(onSlotSwitched).toHaveBeenCalledWith("newslot", newStatus);
    });

    it("surfaces the 'pending_uploads' error inline", async () => {
      vi.useFakeTimers();
      try {
        vi.mocked(backend.switchSlot).mockResolvedValue({
          success: false,
          reason: "pending_uploads",
        });
        const { container, getByText } = render(<SavesTab {...defaultProps()} />);
        fireEvent.click(getByText("+ New Slot"));
        await act(async () => {
          await newSlotModalSubmit()?.("blocked");
          await vi.advanceTimersByTimeAsync(0);
        });
        expect(container.textContent).toContain("Sync your saves first — local changes haven't been uploaded");
      } finally {
        vi.useRealTimers();
      }
    });

    it("clears the inline error after 5 seconds", async () => {
      vi.useFakeTimers();
      try {
        vi.mocked(backend.switchSlot).mockResolvedValue({
          success: false,
          reason: "pending_uploads",
        });
        const { container, getByText } = render(<SavesTab {...defaultProps()} />);
        fireEvent.click(getByText("+ New Slot"));
        await act(async () => {
          await newSlotModalSubmit()?.("blocked");
          await vi.advanceTimersByTimeAsync(0);
        });
        expect(container.textContent).toContain("Sync your saves first — local changes haven't been uploaded");
        await act(async () => {
          await vi.advanceTimersByTimeAsync(5001);
        });
        expect(container.textContent).not.toContain("Sync your saves first — local changes haven't been uploaded");
      } finally {
        vi.useRealTimers();
      }
    });

    it("surfaces the 'server_unreachable' error inline", async () => {
      vi.useFakeTimers();
      try {
        vi.mocked(backend.switchSlot).mockResolvedValue({
          success: false,
          reason: "server_unreachable",
        });
        const { container, getByText } = render(<SavesTab {...defaultProps()} />);
        fireEvent.click(getByText("+ New Slot"));
        await act(async () => {
          await newSlotModalSubmit()?.("offline");
          await vi.advanceTimersByTimeAsync(0);
        });
        expect(container.textContent).toContain("Can't switch — RomM server is not reachable");
      } finally {
        vi.useRealTimers();
      }
    });

    it("surfaces the generic 'Failed to create slot' on an unknown reason", async () => {
      vi.useFakeTimers();
      try {
        vi.mocked(backend.switchSlot).mockResolvedValue({
          success: false,
          reason: "sync_disabled",
        });
        const { container, getByText } = render(<SavesTab {...defaultProps()} />);
        fireEvent.click(getByText("+ New Slot"));
        await act(async () => {
          await newSlotModalSubmit()?.("named");
          await vi.advanceTimersByTimeAsync(0);
        });
        expect(container.textContent).toContain("Failed to create slot");
      } finally {
        vi.useRealTimers();
      }
    });

    it("surfaces the catch-all error when switchSlot throws", async () => {
      vi.useFakeTimers();
      try {
        vi.mocked(backend.switchSlot).mockRejectedValue(new Error("boom"));
        const { container, getByText } = render(<SavesTab {...defaultProps()} />);
        fireEvent.click(getByText("+ New Slot"));
        await act(async () => {
          await newSlotModalSubmit()?.("named");
          await vi.advanceTimersByTimeAsync(0);
        });
        expect(container.textContent).toContain("An error occurred while creating the slot");
      } finally {
        vi.useRealTimers();
      }
    });

    it("clears the catch-all error after 5 seconds", async () => {
      vi.useFakeTimers();
      try {
        vi.mocked(backend.switchSlot).mockRejectedValue(new Error("boom"));
        const { container, getByText } = render(<SavesTab {...defaultProps()} />);
        fireEvent.click(getByText("+ New Slot"));
        await act(async () => {
          await newSlotModalSubmit()?.("named");
          await vi.advanceTimersByTimeAsync(0);
        });
        expect(container.textContent).toContain("An error occurred while creating the slot");
        await act(async () => {
          await vi.advanceTimersByTimeAsync(5001);
        });
        expect(container.textContent).not.toContain("An error occurred while creating the slot");
      } finally {
        vi.useRealTimers();
      }
    });

    it("clears any pending 5s timer on unmount", async () => {
      vi.useFakeTimers();
      try {
        const setSpy = vi.spyOn(globalThis, "setTimeout");
        const clearSpy = vi.spyOn(globalThis, "clearTimeout");
        vi.mocked(backend.switchSlot).mockResolvedValue({
          success: false,
          reason: "server_unreachable",
        } as SwitchSlotResponse);

        const { getByText, unmount } = render(<SavesTab {...defaultProps()} />);
        fireEvent.click(getByText("+ New Slot"));
        await act(async () => {
          await newSlotModalSubmit()?.("named");
          await vi.advanceTimersByTimeAsync(0);
        });

        // Capture the timer id of the most-recent 5000ms scheduling — that's
        // the one the unmount cleanup must clear. Filtering by delay avoids
        // happy-dom / React internal timers.
        const scheduledIds = setSpy.mock.results
          .filter((_, i) => setSpy.mock.calls[i]?.[1] === 5000)
          .map((r) => r.value as ReturnType<typeof setTimeout>);
        const expectedId = scheduledIds[scheduledIds.length - 1];
        expect(expectedId).toBeDefined();

        unmount();

        expect(clearSpy).toHaveBeenCalledWith(expectedId);
      } finally {
        vi.useRealTimers();
      }
    });
  });

  describe("event dispatch — version restored + slot deleted", () => {
    it("dispatches romm_data_changed when a child SlotPanel calls onVersionRestored", async () => {
      render(<SavesTab {...defaultProps({ availableSlots: [makeSlot()] })} />);
      await flushAsync();
      const listener = vi.fn();
      globalThis.addEventListener("romm_data_changed", listener);
      try {
        const onVersionRestored = capturedSlotPanelProps[0]?.onVersionRestored;
        act(() => {
          onVersionRestored?.();
        });
        expect(listener).toHaveBeenCalledTimes(1);
        const event = listener.mock.calls[0]?.[0] as CustomEvent;
        expect(event.detail).toEqual({ type: "save_sync", rom_id: 1 });
      } finally {
        globalThis.removeEventListener("romm_data_changed", listener);
      }
    });

    it("re-renders SlotPanel children after onVersionRestored", async () => {
      // Render with two distinct slots so each SavesTab render produces 2
      // captured-prop entries. The state bump in onVersionRestored triggers a
      // re-render of all panels — the captured-props array grows by 2, not 1.
      // The key-change behavior itself (panel-${slot}-${versionHistoryKey})
      // forces a remount which resets SlotPanel-local state — that effect is
      // verified manually in integration testing, not asserted here.
      const slots = [makeSlot({ slot: "a" }), makeSlot({ slot: "b" })];
      render(<SavesTab {...defaultProps({ activeSlot: "a", availableSlots: slots })} />);
      // Baseline is taken after the mount probe settles, so the act() below is
      // the only thing that can add captured props.
      await flushAsync();
      const initialCount = capturedSlotPanelProps.length;
      expect(initialCount).toBe(2);
      act(() => {
        capturedSlotPanelProps[0]?.onVersionRestored();
      });
      expect(capturedSlotPanelProps.length).toBe(initialCount + 2);
    });

    it("dispatches romm_data_changed when a child SlotPanel calls onSlotDeleted", async () => {
      render(<SavesTab {...defaultProps({ availableSlots: [makeSlot()] })} />);
      await flushAsync();
      const listener = vi.fn();
      globalThis.addEventListener("romm_data_changed", listener);
      try {
        act(() => {
          capturedSlotPanelProps[0]?.onSlotDeleted();
        });
        expect(listener).toHaveBeenCalledTimes(1);
        const event = listener.mock.calls[0]?.[0] as CustomEvent;
        expect(event.detail).toEqual({ type: "save_sync", rom_id: 1 });
      } finally {
        globalThis.removeEventListener("romm_data_changed", listener);
      }
    });

    it("forwards the parent's onSlotSwitched through to the SlotPanel", async () => {
      const onSlotSwitched = vi.fn();
      render(
        <SavesTab
          {...defaultProps({
            onSlotSwitched,
            availableSlots: [makeSlot()],
          })}
        />,
      );
      expect(capturedSlotPanelProps[0]?.onSlotSwitched).toBe(onSlotSwitched);
      await flushAsync();
    });
  });

  describe("stranded-version banner (#1298)", () => {
    const STRANDED_COPY = 'Version "Game (USA)" has saves that were never uploaded — switch back to sync them.';

    function makeVersion(
      overrides: Partial<import("../api/backend").VersionInfo>,
    ): import("../api/backend").VersionInfo {
      return {
        rom_id: 0,
        name: "",
        label: "",
        regions: [],
        languages: [],
        revision: "",
        tags: [],
        synced: true,
        installed: false,
        active: false,
        is_default: false,
        switchable: true,
        vanished: false,
        ...overrides,
      };
    }

    // Active = Japan (installed); an inactive USA build is also on disk.
    const listWithStranded: import("../api/backend").VersionList = {
      multi_version: true,
      bound_vanished: false,
      versions: [
        makeVersion({ rom_id: 2, label: "Game (Japan)", active: true, installed: true }),
        makeVersion({ rom_id: 5, label: "Game (USA)", active: false, installed: true }),
      ],
    };
    // Active = Japan (installed); the other version is NOT downloaded.
    const listNoStranded: import("../api/backend").VersionList = {
      multi_version: true,
      bound_vanished: false,
      versions: [
        makeVersion({ rom_id: 2, label: "Game (Japan)", active: true, installed: true }),
        makeVersion({ rom_id: 5, label: "Game (USA)", active: false, installed: false }),
      ],
    };

    it("shows the banner with the verbatim copy when an inactive installed version drifts", async () => {
      vi.mocked(backend.getVersionList).mockResolvedValue(listWithStranded);
      vi.mocked(backend.checkLocalDrift).mockResolvedValue({ drifted: true, rom_id: 5 });

      const { container } = render(<SavesTab {...defaultProps({ romId: 2 })} />);

      await waitFor(() => expect(container.textContent).toContain(STRANDED_COPY));
      expect(backend.checkLocalDrift).toHaveBeenCalledWith(5);
    });

    it("hides the banner when no inactive version is installed", async () => {
      vi.mocked(backend.getVersionList).mockResolvedValue(listNoStranded);

      const { container } = render(<SavesTab {...defaultProps({ romId: 2 })} />);

      await waitFor(() => expect(vi.mocked(backend.getVersionList)).toHaveBeenCalled());
      expect(container.textContent).not.toContain("never uploaded");
      // No inactive install → the drift probe is never even fired.
      expect(backend.checkLocalDrift).not.toHaveBeenCalled();
    });

    it("hides the banner when the inactive install has no drift", async () => {
      vi.mocked(backend.getVersionList).mockResolvedValue(listWithStranded);
      vi.mocked(backend.checkLocalDrift).mockResolvedValue({ drifted: false, rom_id: 5 });

      const { container } = render(<SavesTab {...defaultProps({ romId: 2 })} />);

      await waitFor(() => expect(backend.checkLocalDrift).toHaveBeenCalledWith(5));
      expect(container.textContent).not.toContain("never uploaded");
    });

    it("re-runs the check when the bound version changes (version switch)", async () => {
      // First (romId=2): nothing stranded. After the switch (romId=99): the USA
      // build is now the inactive-installed drifted sibling → banner appears.
      vi.mocked(backend.getVersionList).mockResolvedValueOnce(listNoStranded).mockResolvedValue(listWithStranded);
      vi.mocked(backend.checkLocalDrift).mockResolvedValue({ drifted: true, rom_id: 5 });

      const { container, rerender } = render(<SavesTab {...defaultProps({ romId: 2 })} />);
      await waitFor(() => expect(vi.mocked(backend.getVersionList)).toHaveBeenCalledTimes(1));
      expect(container.textContent).not.toContain("never uploaded");

      rerender(<SavesTab {...defaultProps({ romId: 99 })} />);
      await waitFor(() => expect(container.textContent).toContain(STRANDED_COPY));
    });

    it("probes every inactive install and surfaces the first DRIFTED one", async () => {
      // Two inactive installed siblings: the USA build is clean, the Europe build
      // drifted. The banner must probe past USA and name Europe.
      vi.mocked(backend.getVersionList).mockResolvedValue({
        multi_version: true,
        bound_vanished: false,
        versions: [
          makeVersion({ rom_id: 2, label: "Game (Japan)", active: true, installed: true }),
          makeVersion({ rom_id: 5, label: "Game (USA)", active: false, installed: true }),
          makeVersion({ rom_id: 6, label: "Game (Europe)", active: false, installed: true }),
        ],
      });
      vi.mocked(backend.checkLocalDrift).mockImplementation((romId: number) =>
        Promise.resolve({ drifted: romId === 6, rom_id: romId }),
      );

      const { container } = render(<SavesTab {...defaultProps({ romId: 2 })} />);

      await waitFor(() =>
        expect(container.textContent).toContain(
          'Version "Game (Europe)" has saves that were never uploaded — switch back to sync them.',
        ),
      );
      // Both were probed (USA first, then Europe); the clean USA build isn't named.
      expect(backend.checkLocalDrift).toHaveBeenCalledWith(5);
      expect(backend.checkLocalDrift).toHaveBeenCalledWith(6);
      expect(container.textContent).not.toContain("Game (USA)");
    });

    it("skips vanished installs and continues to a later live drifted version", async () => {
      vi.mocked(backend.getVersionList).mockResolvedValue({
        multi_version: true,
        bound_vanished: false,
        versions: [
          makeVersion({ rom_id: 2, label: "Game (Japan)", active: true, installed: true }),
          makeVersion({ rom_id: 5, label: "Gone (USA)", installed: true, vanished: true }),
          makeVersion({ rom_id: 6, label: "Game (Europe)", installed: true }),
        ],
      });
      vi.mocked(backend.checkLocalDrift).mockResolvedValue({ drifted: true, rom_id: 6 });

      const { container } = render(<SavesTab {...defaultProps({ romId: 2 })} />);

      await waitFor(() =>
        expect(container.textContent).toContain(
          'Version "Game (Europe)" has saves that were never uploaded — switch back to sync them.',
        ),
      );
      expect(backend.checkLocalDrift).not.toHaveBeenCalledWith(5);
      expect(backend.checkLocalDrift).toHaveBeenCalledWith(6);
      expect(container.textContent).not.toContain("Gone (USA)");
    });

    it("logs a warning and hides the banner when the version-list probe rejects", async () => {
      const logWarnSpy = vi.spyOn(backend, "logWarn").mockImplementation(() => {});
      try {
        vi.mocked(backend.getVersionList).mockRejectedValue(new Error("offline"));

        const { container } = render(<SavesTab {...defaultProps({ romId: 2 })} />);

        await waitFor(() =>
          expect(logWarnSpy).toHaveBeenCalledWith(expect.stringContaining("stranded-version check failed")),
        );
        expect(container.textContent).not.toContain("never uploaded");
      } finally {
        logWarnSpy.mockRestore();
      }
    });
  });
});
