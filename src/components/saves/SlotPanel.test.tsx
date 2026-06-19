import { describe, it, expect, beforeEach, vi } from "vitest";
import { render, fireEvent, act } from "@testing-library/react";
import { createElement } from "react";
import { SlotPanel } from "./SlotPanel";
import * as backend from "../../api/backend";
import { toaster } from "@decky/api";
import { showModal } from "@decky/ui";
import type { SaveStatus, SaveSlotSummary, SlotSaveFile } from "../../types";

// showModal in the @decky/ui mock receives a React element created by
// createElement(ConfirmModal, props). We capture that element so tests can
// read `.props.onOK`, `.props.strDescription`, etc.
type AnyProps = Record<string, unknown> & { children?: unknown };
interface ConfirmModalProps {
  onOK?: () => void | Promise<void>;
  strDescription?: string;
  strTitle?: string;
  strOKButtonText?: string;
}

vi.mock("@decky/ui", () => {
  // ConfirmModal is just a marker component — never actually rendered in tests.
  // showModal captures the element and tests pull props off it directly.
  const ConfirmModal = (p: AnyProps) => createElement("div", {}, p.children as never);
  return {
    ConfirmModal,
    DialogButton: ({
      children,
      onClick,
      disabled,
    }: AnyProps & {
      onClick?: () => void;
      disabled?: boolean;
    }) => createElement("button", { onClick, disabled }, children as never),
    // Used transitively by InactiveSlotBody, which SlotPanel renders.
    Focusable: (p: AnyProps) => createElement("div", {}, p.children as never),
    showModal: vi.fn(),
  };
});

function lastConfirmModalProps(): ConfirmModalProps | null {
  const calls = vi.mocked(showModal).mock.calls;
  if (calls.length === 0) return null;
  const el = calls[calls.length - 1]?.[0] as { props?: ConfirmModalProps } | undefined;
  return el?.props ?? null;
}

// Stub VersionHistoryPanel so we don't need to wire its own callable mocks.
// We render a tiny marker so tests can assert it appeared per file.
vi.mock("./VersionHistoryPanel", () => ({
  VersionHistoryPanel: (p: { filename: string; isOffline: boolean }) =>
    createElement("div", { "data-testid": `vhp-${p.filename}`, "data-offline": String(p.isOffline) }),
}));

function makeSummary(overrides: Partial<SaveSlotSummary> = {}): SaveSlotSummary {
  return {
    slot: "default",
    source: "local",
    count: 0,
    latest_updated_at: null,
    ...overrides,
  };
}

function makeStatus(overrides: Partial<SaveStatus> = {}): SaveStatus {
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

function defaultProps(overrides: Partial<React.ComponentProps<typeof SlotPanel>> = {}) {
  return {
    romId: 1,
    slot: makeSummary(),
    isActive: false,
    defaultExpanded: false,
    saveStatus: null,
    conflicts: [],
    isOffline: false,
    onSlotSwitched: vi.fn(),
    onVersionRestored: vi.fn(),
    onSlotDeleted: vi.fn(),
    ...overrides,
  };
}

const flushAsync = () => new Promise((r) => setTimeout(r, 0));

describe("SlotPanel", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  describe("expand/collapse", () => {
    it("renders collapsed by default", () => {
      const { container } = render(<SlotPanel {...defaultProps()} />);
      expect(container.textContent).toContain("▸");
      expect(container.textContent).not.toContain("▾");
    });

    it("renders expanded when defaultExpanded is true", () => {
      const { container } = render(<SlotPanel {...defaultProps({ defaultExpanded: true })} />);
      expect(container.textContent).toContain("▾");
    });

    it("toggles expanded state when the header is clicked", async () => {
      const { container } = render(<SlotPanel {...defaultProps()} />);
      const header = container.querySelector("button");
      if (!header) throw new Error("no header button");
      fireEvent.click(header);
      await flushAsync();
      expect(container.textContent).toContain("▾");
      fireEvent.click(header);
      await flushAsync();
      expect(container.textContent).toContain("▸");
    });

    it("calls getSlotSaves exactly once on first expand for an inactive slot (caches)", async () => {
      vi.mocked(backend.getSlotSaves).mockResolvedValue({
        success: true,
        slot: "default",
        saves: [],
      });
      const { container } = render(<SlotPanel {...defaultProps()} />);
      const header = container.querySelector("button");
      if (!header) throw new Error("no header");
      fireEvent.click(header);
      await flushAsync();
      fireEvent.click(header); // collapse
      fireEvent.click(header); // expand again — should NOT refetch (cached)
      await flushAsync();
      expect(vi.mocked(backend.getSlotSaves)).toHaveBeenCalledTimes(1);
    });

    it("does NOT call getSlotSaves when expanding an active slot", async () => {
      const { container } = render(<SlotPanel {...defaultProps({ isActive: true, saveStatus: makeStatus() })} />);
      const header = container.querySelector("button");
      if (!header) throw new Error("no header");
      fireEvent.click(header);
      await flushAsync();
      expect(vi.mocked(backend.getSlotSaves)).not.toHaveBeenCalled();
    });

    it("falls back to an empty list when getSlotSaves returns success=false", async () => {
      vi.mocked(backend.getSlotSaves).mockResolvedValue({
        success: false,
        slot: "default",
        saves: [],
      });
      const { container } = render(<SlotPanel {...defaultProps()} />);
      fireEvent.click(container.querySelector("button")!);
      await flushAsync();
      expect(container.textContent).toContain("No saves in this slot");
    });

    it("falls back to an empty list when getSlotSaves throws", async () => {
      vi.mocked(backend.getSlotSaves).mockRejectedValue(new Error("network"));
      const { container } = render(<SlotPanel {...defaultProps()} />);
      fireEvent.click(container.querySelector("button")!);
      await flushAsync();
      expect(container.textContent).toContain("No saves in this slot");
    });
  });

  describe("active slot body", () => {
    it("renders one SaveFileRow + VersionHistoryPanel per file", () => {
      const status = makeStatus({
        files: [
          {
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
          },
          {
            filename: "b.srm",
            local_path: "/data/b.srm",
            local_hash: "h",
            local_mtime: "2025-06-15T10:00:00Z",
            local_size: 100,
            server_save_id: 2,
            server_file_name: null,
            server_emulator: null,
            server_updated_at: null,
            server_size: null,
            last_sync_at: null,
            status: "synced",
          },
        ],
      });
      const { container, queryByTestId } = render(
        <SlotPanel
          {...defaultProps({
            isActive: true,
            defaultExpanded: true,
            saveStatus: status,
          })}
        />,
      );
      expect(container.textContent).toContain("a.srm");
      expect(container.textContent).toContain("b.srm");
      expect(queryByTestId("vhp-a.srm")).not.toBeNull();
      expect(queryByTestId("vhp-b.srm")).not.toBeNull();
    });

    it("multi-file slot: shows component list + #908 note and NO VersionHistoryPanel", () => {
      const status = makeStatus({
        files: [
          {
            filename: "rally.bkr",
            local_path: "/data/rally.bkr",
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
          },
        ],
        multi_file: true,
        component_files: ["rally.bcr", "rally.bkr", "rally.smpc"],
        rollback_supported: false,
      });
      const { container, queryByTestId } = render(
        <SlotPanel
          {...defaultProps({
            isActive: true,
            defaultExpanded: true,
            saveStatus: status,
          })}
        />,
      );
      // The component file list is shown.
      expect(container.textContent).toContain("Files in this save (3)");
      expect(container.textContent).toContain("rally.bcr");
      expect(container.textContent).toContain("rally.smpc");
      // The calm #908 note replaces Previous Versions / rollback.
      expect(container.textContent).toContain(
        "This save spans 3 files. Per-version rollback isn't available for multi-file saves yet.",
      );
      // No version-history / rollback control is rendered for the file.
      expect(queryByTestId("vhp-rally.bkr")).toBeNull();
    });

    it("shows 'No save files tracked yet' when active slot has no files", () => {
      const { container } = render(
        <SlotPanel
          {...defaultProps({
            isActive: true,
            defaultExpanded: true,
            saveStatus: makeStatus({ files: [] }),
          })}
        />,
      );
      expect(container.textContent).toContain("No save files tracked yet");
    });

    it("forwards isOffline to VersionHistoryPanel for each file", () => {
      const status = makeStatus({
        files: [
          {
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
          },
        ],
      });
      const { getByTestId } = render(
        <SlotPanel
          {...defaultProps({
            isActive: true,
            defaultExpanded: true,
            saveStatus: status,
            isOffline: true,
          })}
        />,
      );
      expect(getByTestId("vhp-a.srm").getAttribute("data-offline")).toBe("true");
    });
  });

  describe("handleActivate", () => {
    it("calls onSlotSwitched on a successful switch", async () => {
      vi.mocked(backend.getSlotSaves).mockResolvedValue({
        success: true,
        slot: "default",
        saves: [],
      });
      const newStatus = makeStatus({ active_slot: "default" });
      vi.mocked(backend.switchSlot).mockResolvedValue({
        success: true,
        save_status: newStatus,
      });

      const onSlotSwitched = vi.fn();
      const { container, getByText } = render(<SlotPanel {...defaultProps({ onSlotSwitched })} />);
      fireEvent.click(container.querySelector("button")!); // expand
      await flushAsync();
      fireEvent.click(getByText("Activate Slot"));
      await flushAsync();
      await flushAsync();
      expect(onSlotSwitched).toHaveBeenCalledTimes(1);
      expect(onSlotSwitched).toHaveBeenCalledWith("default", newStatus);
    });

    it("shows the 'pending_uploads' error", async () => {
      vi.useFakeTimers();
      try {
        vi.mocked(backend.getSlotSaves).mockResolvedValue({
          success: true,
          slot: "default",
          saves: [],
        });
        vi.mocked(backend.switchSlot).mockResolvedValue({
          success: false,
          reason: "pending_uploads",
        });

        const { container, getByText } = render(<SlotPanel {...defaultProps()} />);
        fireEvent.click(container.querySelector("button")!);
        await act(async () => {
          await vi.advanceTimersByTimeAsync(0);
        });
        fireEvent.click(getByText("Activate Slot"));
        await act(async () => {
          await vi.advanceTimersByTimeAsync(0);
          await vi.advanceTimersByTimeAsync(0);
        });
        expect(container.textContent).toContain("Sync your saves first — local changes haven't been uploaded");
      } finally {
        vi.useRealTimers();
      }
    });

    it("auto-clears the switchError after 5 seconds", async () => {
      vi.useFakeTimers();
      try {
        vi.mocked(backend.getSlotSaves).mockResolvedValue({
          success: true,
          slot: "default",
          saves: [],
        });
        vi.mocked(backend.switchSlot).mockResolvedValue({
          success: false,
          reason: "pending_uploads",
        });

        const { container, getByText } = render(<SlotPanel {...defaultProps()} />);
        fireEvent.click(container.querySelector("button")!);
        await vi.advanceTimersByTimeAsync(0);
        fireEvent.click(getByText("Activate Slot"));
        // Flush the awaited switchSlot promise + the setSwitchError state
        await act(async () => {
          await vi.advanceTimersByTimeAsync(0);
          await vi.advanceTimersByTimeAsync(0);
        });
        expect(container.textContent).toContain("Sync your saves first — local changes haven't been uploaded");
        // The 5s clear timer fires
        await act(async () => {
          await vi.advanceTimersByTimeAsync(5001);
        });
        expect(container.textContent).not.toContain("Sync your saves first — local changes haven't been uploaded");
      } finally {
        vi.useRealTimers();
      }
    });

    it("shows the 'server_unreachable' error", async () => {
      vi.useFakeTimers();
      try {
        vi.mocked(backend.getSlotSaves).mockResolvedValue({
          success: true,
          slot: "default",
          saves: [],
        });
        vi.mocked(backend.switchSlot).mockResolvedValue({
          success: false,
          reason: "server_unreachable",
        });
        const { container, getByText } = render(<SlotPanel {...defaultProps()} />);
        fireEvent.click(container.querySelector("button")!);
        await act(async () => {
          await vi.advanceTimersByTimeAsync(0);
        });
        fireEvent.click(getByText("Activate Slot"));
        await act(async () => {
          await vi.advanceTimersByTimeAsync(0);
          await vi.advanceTimersByTimeAsync(0);
        });
        expect(container.textContent).toContain("Can't switch — RomM server is not reachable");
      } finally {
        vi.useRealTimers();
      }
    });

    it("shows the generic error on unknown reason", async () => {
      vi.useFakeTimers();
      try {
        vi.mocked(backend.getSlotSaves).mockResolvedValue({
          success: true,
          slot: "default",
          saves: [],
        });
        vi.mocked(backend.switchSlot).mockResolvedValue({
          success: false,
          reason: "sync_disabled",
        });
        const { container, getByText } = render(<SlotPanel {...defaultProps()} />);
        fireEvent.click(container.querySelector("button")!);
        await act(async () => {
          await vi.advanceTimersByTimeAsync(0);
        });
        fireEvent.click(getByText("Activate Slot"));
        await act(async () => {
          await vi.advanceTimersByTimeAsync(0);
          await vi.advanceTimersByTimeAsync(0);
        });
        expect(container.textContent).toContain("Failed to switch slot");
      } finally {
        vi.useRealTimers();
      }
    });

    it("catches thrown errors and surfaces an error line", async () => {
      vi.useFakeTimers();
      try {
        vi.mocked(backend.getSlotSaves).mockResolvedValue({
          success: true,
          slot: "default",
          saves: [],
        });
        vi.mocked(backend.switchSlot).mockRejectedValue(new Error("boom"));
        const { container, getByText } = render(<SlotPanel {...defaultProps()} />);
        fireEvent.click(container.querySelector("button")!);
        await act(async () => {
          await vi.advanceTimersByTimeAsync(0);
        });
        fireEvent.click(getByText("Activate Slot"));
        await act(async () => {
          await vi.advanceTimersByTimeAsync(0);
          await vi.advanceTimersByTimeAsync(0);
        });
        expect(container.textContent).toContain("An error occurred while switching slots");
      } finally {
        vi.useRealTimers();
      }
    });
  });

  describe("handleDelete", () => {
    it("toasts the failure reason and skips the confirm modal when getSlotDeleteInfo returns !success", async () => {
      vi.mocked(backend.getSlotSaves).mockResolvedValue({
        success: true,
        slot: "default",
        saves: [],
      });
      vi.mocked(backend.getSlotDeleteInfo).mockResolvedValue({
        success: false,
        reason: "active_slot",
      });
      const { container, getByText } = render(<SlotPanel {...defaultProps()} />);
      fireEvent.click(container.querySelector("button")!);
      await flushAsync();
      fireEvent.click(getByText("Delete Slot"));
      await flushAsync();
      await flushAsync();
      expect(vi.mocked(toaster.toast)).toHaveBeenCalledWith(
        expect.objectContaining({
          body: "Cannot delete the active slot. Switch to a different slot first.",
        }),
      );
      expect(vi.mocked(showModal)).not.toHaveBeenCalled();
    });

    it("opens the ConfirmModal with a server-delete message when source=server and saves > 0", async () => {
      vi.mocked(backend.getSlotSaves).mockResolvedValue({
        success: true,
        slot: "default",
        saves: [],
      });
      vi.mocked(backend.getSlotDeleteInfo).mockResolvedValue({
        success: true,
        slot: "default",
        source: "server",
        server_save_count: 3,
        local_file_count: 2,
      });
      vi.mocked(backend.deleteSlot).mockResolvedValue({
        success: true,
      });

      const onSlotDeleted = vi.fn();
      const { container, getByText } = render(<SlotPanel {...defaultProps({ onSlotDeleted })} />);
      fireEvent.click(container.querySelector("button")!);
      await flushAsync();
      fireEvent.click(getByText("Delete Slot"));
      await flushAsync();
      await flushAsync();

      expect(vi.mocked(showModal)).toHaveBeenCalledTimes(1);
      const modalProps = lastConfirmModalProps();
      expect(modalProps).not.toBeNull();
      expect(modalProps?.strTitle).toBe("Delete Slot");
      expect(modalProps?.strDescription).toContain("3 saves from slot 'default'");
      expect(modalProps?.strDescription).toContain("2 tracked files will be unlinked");
      expect(modalProps?.strDescription).toContain("This cannot be undone.");

      // Run the OK callback
      await modalProps?.onOK?.();
      await flushAsync();
      expect(vi.mocked(backend.deleteSlot)).toHaveBeenCalledWith(1, "default");
      expect(vi.mocked(toaster.toast)).toHaveBeenCalledWith(
        expect.objectContaining({ body: "Slot 'default' deleted" }),
      );
      expect(onSlotDeleted).toHaveBeenCalledTimes(1);
    });

    it("uses 'remove from local config' wording when source=local", async () => {
      vi.mocked(backend.getSlotSaves).mockResolvedValue({
        success: true,
        slot: "default",
        saves: [],
      });
      vi.mocked(backend.getSlotDeleteInfo).mockResolvedValue({
        success: true,
        slot: "default",
        source: "local",
        server_save_count: 0,
        local_file_count: 1,
      });

      const { container, getByText } = render(<SlotPanel {...defaultProps()} />);
      fireEvent.click(container.querySelector("button")!);
      await flushAsync();
      fireEvent.click(getByText("Delete Slot"));
      await flushAsync();
      await flushAsync();
      const modalProps = lastConfirmModalProps();
      expect(modalProps?.strDescription).toContain("remove slot 'default' from your local configuration");
      expect(modalProps?.strDescription).toContain("1 tracked file will be unlinked");
    });

    it("toasts the failure message when deleteSlot returns success=false", async () => {
      vi.mocked(backend.getSlotSaves).mockResolvedValue({
        success: true,
        slot: "default",
        saves: [],
      });
      vi.mocked(backend.getSlotDeleteInfo).mockResolvedValue({
        success: true,
        slot: "default",
        source: "local",
      });
      vi.mocked(backend.deleteSlot).mockResolvedValue({
        success: false,
        message: "couldn't reach server",
      });

      const onSlotDeleted = vi.fn();
      const { container, getByText } = render(<SlotPanel {...defaultProps({ onSlotDeleted })} />);
      fireEvent.click(container.querySelector("button")!);
      await flushAsync();
      fireEvent.click(getByText("Delete Slot"));
      await flushAsync();
      await flushAsync();

      await lastConfirmModalProps()?.onOK?.();
      await flushAsync();
      expect(vi.mocked(toaster.toast)).toHaveBeenCalledWith(expect.objectContaining({ body: "couldn't reach server" }));
      expect(onSlotDeleted).not.toHaveBeenCalled();
    });

    it("toasts a generic message when deleteSlot throws", async () => {
      vi.mocked(backend.getSlotSaves).mockResolvedValue({
        success: true,
        slot: "default",
        saves: [],
      });
      vi.mocked(backend.getSlotDeleteInfo).mockResolvedValue({
        success: true,
        slot: "default",
        source: "local",
      });
      vi.mocked(backend.deleteSlot).mockRejectedValue(new Error("network"));

      const { container, getByText } = render(<SlotPanel {...defaultProps()} />);
      fireEvent.click(container.querySelector("button")!);
      await flushAsync();
      fireEvent.click(getByText("Delete Slot"));
      await flushAsync();
      await flushAsync();

      await lastConfirmModalProps()?.onOK?.();
      await flushAsync();
      expect(vi.mocked(toaster.toast)).toHaveBeenCalledWith(
        expect.objectContaining({ body: "An error occurred while deleting the slot" }),
      );
    });

    it("toasts a generic 'Failed to load slot info' when getSlotDeleteInfo throws", async () => {
      vi.mocked(backend.getSlotSaves).mockResolvedValue({
        success: true,
        slot: "default",
        saves: [],
      });
      vi.mocked(backend.getSlotDeleteInfo).mockRejectedValue(new Error("network"));

      const { container, getByText } = render(<SlotPanel {...defaultProps()} />);
      fireEvent.click(container.querySelector("button")!);
      await flushAsync();
      fireEvent.click(getByText("Delete Slot"));
      await flushAsync();
      await flushAsync();

      expect(vi.mocked(toaster.toast)).toHaveBeenCalledWith(
        expect.objectContaining({ body: "Failed to load slot info" }),
      );
      expect(vi.mocked(showModal)).not.toHaveBeenCalled();
    });
  });

  describe("badges and summary line", () => {
    it("renders the 'local' source badge for source=local", () => {
      const { container } = render(<SlotPanel {...defaultProps({ slot: makeSummary({ source: "local" }) })} />);
      const badge = container.querySelector(".romm-slot-badge-local");
      expect(badge).not.toBeNull();
      expect(badge?.textContent).toBe("local");
    });

    it("renders the 'server' source badge for source=server", () => {
      const { container } = render(<SlotPanel {...defaultProps({ slot: makeSummary({ source: "server" }) })} />);
      const badge = container.querySelector(".romm-slot-badge-server");
      expect(badge).not.toBeNull();
      expect(badge?.textContent).toBe("server");
    });

    it("shows the 'active' badge only when isActive is true", () => {
      const { container, rerender } = render(<SlotPanel {...defaultProps()} />);
      expect(container.querySelector(".romm-slot-badge-active")).toBeNull();
      rerender(<SlotPanel {...defaultProps({ isActive: true, saveStatus: makeStatus() })} />);
      expect(container.querySelector(".romm-slot-badge-active")).not.toBeNull();
    });

    it("shows the sync summary line only when active AND syncSummaryText is non-null", () => {
      // Inactive: no summary line
      const { container, rerender } = render(<SlotPanel {...defaultProps()} />);
      expect(container.querySelector(".romm-slot-sync-summary")).toBeNull();
      // Active with empty files: "No saves found" text appears
      rerender(<SlotPanel {...defaultProps({ isActive: true, saveStatus: makeStatus() })} />);
      const summary = container.querySelector(".romm-slot-sync-summary");
      expect(summary).not.toBeNull();
      expect(summary?.textContent).toBe("No saves found");
    });

    it("renders the slot count using slot.count when inactive and no slot files loaded", () => {
      const { container } = render(<SlotPanel {...defaultProps({ slot: makeSummary({ count: 5 }) })} />);
      expect(container.textContent).toContain("5 saves");
    });

    it("uses singular 'save' wording for count === 1", () => {
      const { container } = render(<SlotPanel {...defaultProps({ slot: makeSummary({ count: 1 }) })} />);
      expect(container.textContent).toContain("1 save");
      expect(container.textContent).not.toContain("1 saves");
    });
  });

  it("renders '(no slot)' for an empty slot name", () => {
    const { container } = render(<SlotPanel {...defaultProps({ slot: makeSummary({ slot: "" }) })} />);
    expect(container.textContent).toContain("Legacy (no slot)");
  });

  it("loads and renders inactive slot files when expanded", async () => {
    const files: SlotSaveFile[] = [{ id: 1, filename: "remote-a.srm", size: 1024, updated_at: "", emulator: "mgba" }];
    vi.mocked(backend.getSlotSaves).mockResolvedValue({
      success: true,
      slot: "default",
      saves: files,
    });
    const { container } = render(<SlotPanel {...defaultProps()} />);
    fireEvent.click(container.querySelector("button")!);
    await flushAsync();
    expect(container.textContent).toContain("remote-a.srm");
  });
});
