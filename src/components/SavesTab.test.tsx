import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { render, fireEvent, act } from "@testing-library/react";
import { createElement, type ComponentProps, type ReactElement } from "react";
import { SavesTab } from "./SavesTab";
import * as backend from "../api/backend";
import { showModal } from "@decky/ui";
import { getRommConnectionState } from "../utils/connectionState";
import type {
  SaveStatus,
  SaveSlotSummary,
  SaveFileStatus,
  SwitchSlotResponse,
} from "../types";
// Type-only — vi.mock("./saves/SlotPanel", ...) below replaces the runtime
// implementation, but the prop interface comes from the real component so
// captured-prop assertions stay in sync as SlotPanel evolves.
import type { SlotPanel } from "./saves/SlotPanel";
import {
  installDomEventListenerSpy,
  uninstallDomEventListenerSpy,
  domListenerCount,
} from "../test-utils/dom-event-listener-spy";

// showModal from the global @decky/ui mock receives a React element created via
// createElement(NewSlotModal, props) or createElement(ConfirmModal, props).
// Tests pull `props.onSubmit` / `props.onOK` off the captured element to drive
// the new-slot + legacy-confirm flows.
interface NewSlotModalProps {
  onSubmit?: (name: string) => void | Promise<void>;
}
interface ConfirmModalProps {
  onOK?: () => void | Promise<void>;
  strTitle?: string;
  strDescription?: string;
}

vi.mock("../utils/connectionState", () => ({
  getRommConnectionState: vi.fn(() => "connected"),
}));

// Stub NewSlotModal — its own tests cover the text-field + trim behavior.
// SavesTab only cares that it gets rendered with an onSubmit, which we capture
// via showModal.mock.calls[N][0].props.onSubmit.
vi.mock("./saves/NewSlotModal", () => ({
  NewSlotModal: (_p: NewSlotModalProps) =>
    createElement("div", { "data-testid": "new-slot-modal" }),
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
// without dragging in the full DialogButton render tree.
vi.mock("./saves/SaveFileRow", () => ({
  renderSaveFileRow: (f: SaveFileStatus) =>
    createElement("div", { "data-testid": `save-file-row-${f.filename}` }, f.filename),
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
    romId: 1,
    saveStatus: null,
    conflicts: [],
    activeSlot: "default",
    availableSlots: [],
    slotsLoading: false,
    onSlotSwitched: vi.fn(),
    ...overrides,
  };
}

// Helper: pull the onSubmit prop off the NewSlotModal element passed to
// showModal at call index `idx`. Mirrors SlotPanel.test's
// `lastConfirmModalProps()` helper but for the named-arg flow we own here.
function newSlotModalSubmit(idx = 0): ((name: string) => Promise<void>) | undefined {
  const calls = vi.mocked(showModal).mock.calls;
  const el = calls[idx]?.[0] as ReactElement<NewSlotModalProps> | undefined;
  return el?.props.onSubmit as ((name: string) => Promise<void>) | undefined;
}

function lastConfirmModalProps(): ConfirmModalProps | null {
  const calls = vi.mocked(showModal).mock.calls;
  if (calls.length === 0) return null;
  const el = calls[calls.length - 1]?.[0] as ReactElement<ConfirmModalProps> | undefined;
  return el?.props ?? null;
}

describe("SavesTab", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    capturedSlotPanelProps = [];
    vi.mocked(getRommConnectionState).mockReturnValue("connected");
    installDomEventListenerSpy();
  });

  afterEach(() => {
    uninstallDomEventListenerSpy();
  });

  describe("loading state", () => {
    it("renders the loading message when slotsLoading is true", () => {
      const { container, queryByTestId } = render(
        <SavesTab {...defaultProps({ slotsLoading: true })} />,
      );
      expect(container.textContent).toContain("Loading slots...");
      expect(queryByTestId("slot-panel-default")).toBeNull();
    });

    it("still renders the offline banner alongside the loading message", () => {
      vi.mocked(getRommConnectionState).mockReturnValue("offline");
      const { container } = render(
        <SavesTab {...defaultProps({ slotsLoading: true })} />,
      );
      expect(container.textContent).toContain("Loading slots...");
      expect(container.textContent).toContain("RomM is offline");
    });
  });

  describe("offline banner", () => {
    it("does not render when connection state is 'connected'", () => {
      const { container } = render(<SavesTab {...defaultProps()} />);
      expect(container.textContent).not.toContain("RomM is offline");
    });

    it("renders when getRommConnectionState() returns 'offline' at mount", () => {
      vi.mocked(getRommConnectionState).mockReturnValue("offline");
      const { container } = render(<SavesTab {...defaultProps()} />);
      expect(container.textContent).toContain("RomM is offline");
    });

    it("appears on a romm_connection_changed event with state=offline", () => {
      const { container } = render(<SavesTab {...defaultProps()} />);
      expect(container.textContent).not.toContain("RomM is offline");
      act(() => {
        globalThis.dispatchEvent(
          new CustomEvent("romm_connection_changed", { detail: { state: "offline" } }),
        );
      });
      expect(container.textContent).toContain("RomM is offline");
    });

    it("clears on a romm_connection_changed event with state=connected", () => {
      vi.mocked(getRommConnectionState).mockReturnValue("offline");
      const { container } = render(<SavesTab {...defaultProps()} />);
      expect(container.textContent).toContain("RomM is offline");
      act(() => {
        globalThis.dispatchEvent(
          new CustomEvent("romm_connection_changed", { detail: { state: "connected" } }),
        );
      });
      expect(container.textContent).not.toContain("RomM is offline");
    });

    it("removes its connection-changed listener on unmount", () => {
      const before = domListenerCount("romm_connection_changed");
      const { unmount } = render(<SavesTab {...defaultProps()} />);
      expect(domListenerCount("romm_connection_changed")).toBe(before + 1);
      unmount();
      expect(domListenerCount("romm_connection_changed")).toBe(before);
    });

    it("forwards isOffline down to SlotPanel children", () => {
      vi.mocked(getRommConnectionState).mockReturnValue("offline");
      render(
        <SavesTab
          {...defaultProps({
            availableSlots: [makeSlot()],
          })}
        />,
      );
      expect(capturedSlotPanelProps[0]?.isOffline).toBe(true);
    });
  });

  describe("legacy-mode warning + files section", () => {
    it("renders the legacy warning when activeSlot is null", () => {
      const { container } = render(
        <SavesTab {...defaultProps({ activeSlot: null })} />,
      );
      expect(container.textContent).toContain("This game uses legacy mode");
    });

    it("does NOT render the legacy warning when activeSlot is a real slot", () => {
      const { container } = render(<SavesTab {...defaultProps()} />);
      expect(container.textContent).not.toContain("This game uses legacy mode");
    });

    it("renders legacy save-file rows when activeSlot is null and saveStatus has files", () => {
      const status = makeSaveStatus({
        files: [makeSaveFile({ filename: "a.srm" }), makeSaveFile({ filename: "b.srm" })],
      });
      const { queryByTestId } = render(
        <SavesTab
          {...defaultProps({ activeSlot: null, saveStatus: status })}
        />,
      );
      expect(queryByTestId("save-file-row-a.srm")).not.toBeNull();
      expect(queryByTestId("save-file-row-b.srm")).not.toBeNull();
    });

    it("renders the 'No save files tracked yet' empty state when activeSlot is null and no files", () => {
      const { container } = render(
        <SavesTab
          {...defaultProps({
            activeSlot: null,
            saveStatus: makeSaveStatus({ files: [] }),
          })}
        />,
      );
      expect(container.textContent).toContain("No save files tracked yet");
    });

    it("renders the empty state when activeSlot is null and saveStatus is null", () => {
      const { container } = render(
        <SavesTab {...defaultProps({ activeSlot: null, saveStatus: null })} />,
      );
      expect(container.textContent).toContain("No save files tracked yet");
    });

    it("hides the legacy '' slot panel when activeSlot is null", () => {
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
    });
  });

  describe("slot sorting + active-slot synthesis", () => {
    it("sorts active slot first, then alphabetically", () => {
      render(
        <SavesTab
          {...defaultProps({
            activeSlot: "b",
            availableSlots: [
              makeSlot({ slot: "c" }),
              makeSlot({ slot: "a" }),
              makeSlot({ slot: "b" }),
            ],
          })}
        />,
      );
      const order = capturedSlotPanelProps.map((p) => p.slot.slot);
      expect(order).toEqual(["b", "a", "c"]);
    });

    it("marks the active slot with isActive=true and forwards saveStatus/conflicts only to it", () => {
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
    });

    it("synthesizes a placeholder for an active slot missing from availableSlots", () => {
      render(
        <SavesTab
          {...defaultProps({
            activeSlot: "ghost",
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
    });

    it("does NOT synthesize a placeholder when activeSlot is null", () => {
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
    });
  });

  describe("new-slot button", () => {
    it("renders the '+ New Slot' button", () => {
      const { getByText } = render(<SavesTab {...defaultProps()} />);
      expect(getByText("+ New Slot")).not.toBeNull();
    });

    it("opens the NewSlotModal when clicked", () => {
      const { getByText } = render(<SavesTab {...defaultProps()} />);
      fireEvent.click(getByText("+ New Slot"));
      expect(vi.mocked(showModal)).toHaveBeenCalledTimes(1);
    });
  });

  describe("new-slot submit — empty name (legacy mode)", () => {
    it("opens a ConfirmModal with the legacy-mode warning", async () => {
      const { getByText } = render(<SavesTab {...defaultProps()} />);
      fireEvent.click(getByText("+ New Slot"));
      const submit = newSlotModalSubmit();
      await act(async () => {
        await submit?.("");
      });
      // Two showModal calls now: 1) NewSlotModal, 2) ConfirmModal legacy warning.
      expect(vi.mocked(showModal).mock.calls.length).toBe(2);
      const confirmProps = lastConfirmModalProps();
      expect(confirmProps?.strTitle).toBe("Use Legacy Mode?");
      expect(confirmProps?.strDescription).toContain("Legacy mode");
    });

    it("calls switchSlot('') and onSlotSwitched when the legacy confirm is OK'd", async () => {
      const newStatus = makeSaveStatus();
      vi.mocked(backend.switchSlot).mockResolvedValue({
        success: true,
        save_status: newStatus,
      });
      const onSlotSwitched = vi.fn();
      const { getByText } = render(<SavesTab {...defaultProps({ onSlotSwitched })} />);
      fireEvent.click(getByText("+ New Slot"));
      const submit = newSlotModalSubmit();
      await act(async () => {
        await submit?.("");
      });
      await act(async () => {
        await lastConfirmModalProps()?.onOK?.();
      });
      expect(vi.mocked(backend.switchSlot)).toHaveBeenCalledWith(1, "");
      expect(onSlotSwitched).toHaveBeenCalledWith("", newStatus);
    });

    it("logs but does not throw when the legacy switch returns success=false", async () => {
      vi.mocked(backend.switchSlot).mockResolvedValue({
        success: false,
        reason: "sync_disabled",
      });
      const onSlotSwitched = vi.fn();
      const { getByText } = render(<SavesTab {...defaultProps({ onSlotSwitched })} />);
      fireEvent.click(getByText("+ New Slot"));
      await act(async () => {
        await newSlotModalSubmit()?.("");
      });
      await act(async () => {
        await lastConfirmModalProps()?.onOK?.();
      });
      expect(vi.mocked(backend.debugLog)).toHaveBeenCalledWith(
        expect.stringContaining("legacy switch failed"),
      );
      expect(onSlotSwitched).not.toHaveBeenCalled();
    });

    it("logs but does not throw when the legacy switch throws", async () => {
      vi.mocked(backend.switchSlot).mockRejectedValue(new Error("boom"));
      const { getByText } = render(<SavesTab {...defaultProps()} />);
      fireEvent.click(getByText("+ New Slot"));
      await act(async () => {
        await newSlotModalSubmit()?.("");
      });
      await act(async () => {
        await lastConfirmModalProps()?.onOK?.();
      });
      expect(vi.mocked(backend.debugLog)).toHaveBeenCalledWith(
        expect.stringContaining("legacy switch error"),
      );
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
        expect(container.textContent).toContain(
          "Sync your saves first — local changes haven't been uploaded",
        );
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
        expect(container.textContent).toContain(
          "Sync your saves first — local changes haven't been uploaded",
        );
        await act(async () => {
          await vi.advanceTimersByTimeAsync(5001);
        });
        expect(container.textContent).not.toContain(
          "Sync your saves first — local changes haven't been uploaded",
        );
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
        expect(container.textContent).toContain(
          "Can't switch — RomM server is not reachable",
        );
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
        expect(container.textContent).toContain(
          "An error occurred while creating the slot",
        );
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
        expect(container.textContent).toContain(
          "An error occurred while creating the slot",
        );
        await act(async () => {
          await vi.advanceTimersByTimeAsync(5001);
        });
        expect(container.textContent).not.toContain(
          "An error occurred while creating the slot",
        );
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
    it("dispatches romm_data_changed when a child SlotPanel calls onVersionRestored", () => {
      render(
        <SavesTab {...defaultProps({ availableSlots: [makeSlot()] })} />,
      );
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

    it("re-renders SlotPanel children after onVersionRestored", () => {
      // Render with two distinct slots so each SavesTab render produces 2
      // captured-prop entries. The state bump in onVersionRestored triggers a
      // re-render of all panels — the captured-props array grows by 2, not 1.
      // The key-change behavior itself (panel-${slot}-${versionHistoryKey})
      // forces a remount which resets SlotPanel-local state — that effect is
      // verified manually in integration testing, not asserted here.
      const slots = [makeSlot({ slot: "a" }), makeSlot({ slot: "b" })];
      render(
        <SavesTab
          {...defaultProps({ activeSlot: "a", availableSlots: slots })}
        />,
      );
      const initialCount = capturedSlotPanelProps.length;
      expect(initialCount).toBe(2);
      act(() => {
        capturedSlotPanelProps[0]?.onVersionRestored?.();
      });
      expect(capturedSlotPanelProps.length).toBe(initialCount + 2);
    });

    it("dispatches romm_data_changed when a child SlotPanel calls onSlotDeleted", () => {
      render(
        <SavesTab {...defaultProps({ availableSlots: [makeSlot()] })} />,
      );
      const listener = vi.fn();
      globalThis.addEventListener("romm_data_changed", listener);
      try {
        act(() => {
          capturedSlotPanelProps[0]?.onSlotDeleted?.();
        });
        expect(listener).toHaveBeenCalledTimes(1);
        const event = listener.mock.calls[0]?.[0] as CustomEvent;
        expect(event.detail).toEqual({ type: "save_sync", rom_id: 1 });
      } finally {
        globalThis.removeEventListener("romm_data_changed", listener);
      }
    });

    it("forwards the parent's onSlotSwitched through to the SlotPanel", () => {
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
    });
  });
});
