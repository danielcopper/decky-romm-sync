import { describe, it, expect, beforeEach, vi } from "vitest";
import { render, fireEvent, waitFor } from "@testing-library/react";
import { createElement } from "react";
import { VersionHistoryPanel } from "./VersionHistoryPanel";
import * as backend from "../../api/backend";
import { toaster } from "@decky/api";
import { showSyncConflictModal } from "../SyncConflictModal";
import type { SaveVersionEntry, RollbackStatus } from "../../api/backend";

// Override the global DialogButton stub so it forwards `disabled` and we can
// assert it. The global stub only wires onClick.
type AnyProps = Record<string, unknown> & { children?: unknown };
vi.mock("@decky/ui", () => ({
  DialogButton: ({ children, onClick, disabled }: AnyProps & {
    onClick?: () => void;
    disabled?: boolean;
  }) => createElement("button", { onClick, disabled }, children as never),
}));

vi.mock("../SyncConflictModal", () => ({
  showSyncConflictModal: vi.fn(),
}));

function makeVersion(overrides: Partial<SaveVersionEntry> = {}): SaveVersionEntry {
  return {
    id: 11,
    file_name: "save-v1.srm",
    emulator: "mgba",
    updated_at: "2025-06-14T10:00:00Z",
    file_size_bytes: 1024,
    device_syncs: [
      { device_id: "d1", device_name: "deck", is_current: true, last_synced_at: "2025-06-14T10:00:00Z" },
    ],
    uploaded_by_us: true,
    ...overrides,
  };
}

function defaultProps(overrides: Partial<React.ComponentProps<typeof VersionHistoryPanel>> = {}) {
  return {
    romId: 1,
    slot: "default",
    filename: "save.srm",
    isOffline: false,
    onRestored: vi.fn(),
    ...overrides,
  };
}

const flushAsync = () => new Promise((r) => setTimeout(r, 0));

describe("VersionHistoryPanel", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("renders collapsed by default with 'Previous Versions' label", () => {
    const { container, getByText } = render(<VersionHistoryPanel {...defaultProps()} />);
    expect(getByText("Previous Versions")).toBeInTheDocument();
    // ▸ collapsed chevron
    expect(container.textContent).toContain("▸");
    expect(vi.mocked(backend.savesListFileVersions)).not.toHaveBeenCalled();
  });

  it("expands on first click and triggers savesListFileVersions exactly once", async () => {
    vi.mocked(backend.savesListFileVersions).mockResolvedValue({
      status: "ok",
      versions: [makeVersion({ id: 1 })],
    });
    const { getByText, container } = render(<VersionHistoryPanel {...defaultProps()} />);
    fireEvent.click(getByText("Previous Versions"));
    await flushAsync();
    expect(vi.mocked(backend.savesListFileVersions)).toHaveBeenCalledTimes(1);
    expect(vi.mocked(backend.savesListFileVersions)).toHaveBeenCalledWith(
      1,
      "default",
      "save.srm",
    );
    // After load, list count appears in label
    expect(container.textContent).toContain("Previous Versions (1)");
    // ▾ expanded chevron
    expect(container.textContent).toContain("▾");
  });

  it("does not refetch when toggled a second time after a successful load", async () => {
    vi.mocked(backend.savesListFileVersions).mockResolvedValue({
      status: "ok",
      versions: [makeVersion()],
    });
    const { container } = render(<VersionHistoryPanel {...defaultProps()} />);
    const button = container.querySelector("button");
    if (!button) throw new Error("no toggle button");
    fireEvent.click(button);
    await flushAsync();
    fireEvent.click(button); // collapse
    fireEvent.click(button); // expand again
    await flushAsync();
    expect(vi.mocked(backend.savesListFileVersions)).toHaveBeenCalledTimes(1);
  });

  it("skips the fetch when isOffline is true and shows offline body", async () => {
    vi.mocked(backend.savesListFileVersions).mockResolvedValue({
      status: "ok",
      versions: [],
    });
    const { getByText, container } = render(
      <VersionHistoryPanel {...defaultProps({ isOffline: true })} />,
    );
    fireEvent.click(getByText("Previous Versions"));
    await flushAsync();
    expect(vi.mocked(backend.savesListFileVersions)).not.toHaveBeenCalled();
    expect(container.textContent).toContain("Offline — versions unavailable");
  });

  it("shows a loading body while the fetch is in flight", async () => {
    let resolveFetch: (v: { status: "ok"; versions: SaveVersionEntry[] }) => void = () => undefined;
    vi.mocked(backend.savesListFileVersions).mockImplementation(
      () => new Promise((res) => { resolveFetch = res; }),
    );
    const { getByText, container } = render(<VersionHistoryPanel {...defaultProps()} />);
    fireEvent.click(getByText("Previous Versions"));
    await flushAsync();
    expect(container.textContent).toContain("Loading...");
    resolveFetch({ status: "ok", versions: [] });
    await flushAsync();
  });

  it("shows 'No older versions available' when the list is empty", async () => {
    vi.mocked(backend.savesListFileVersions).mockResolvedValue({
      status: "ok",
      versions: [],
    });
    const { getByText, container } = render(<VersionHistoryPanel {...defaultProps()} />);
    fireEvent.click(getByText("Previous Versions"));
    await flushAsync();
    expect(container.textContent).toContain("No older versions available");
  });

  it("shows error body + Retry button when server is unreachable on load", async () => {
    vi.mocked(backend.savesListFileVersions).mockResolvedValue({
      status: "server_unreachable",
      error: "ECONNREFUSED",
    });
    const { getByText, container } = render(<VersionHistoryPanel {...defaultProps()} />);
    fireEvent.click(getByText("Previous Versions"));
    await flushAsync();
    expect(container.textContent).toContain("Couldn't reach RomM. Tap retry.");
    expect(getByText("Retry")).toBeInTheDocument();
  });

  it("Retry button retriggers loadVersions", async () => {
    vi.mocked(backend.savesListFileVersions)
      .mockResolvedValueOnce({ status: "server_unreachable", error: "boom" })
      .mockResolvedValueOnce({ status: "ok", versions: [makeVersion({ id: 7 })] });

    const { getByText, container } = render(<VersionHistoryPanel {...defaultProps()} />);
    fireEvent.click(getByText("Previous Versions"));
    await flushAsync();

    fireEvent.click(getByText("Retry"));
    await flushAsync();
    expect(vi.mocked(backend.savesListFileVersions)).toHaveBeenCalledTimes(2);
    expect(container.textContent).toContain("Previous Versions (1)");
  });

  it("shows error body when the fetch throws", async () => {
    vi.mocked(backend.savesListFileVersions).mockRejectedValue(new Error("network"));
    const { getByText, container } = render(<VersionHistoryPanel {...defaultProps()} />);
    fireEvent.click(getByText("Previous Versions"));
    await flushAsync();
    expect(container.textContent).toContain("Couldn't reach RomM. Tap retry.");
  });

  it("renders version rows with #id · emulator · size and attribution", async () => {
    vi.mocked(backend.savesListFileVersions).mockResolvedValue({
      status: "ok",
      versions: [
        makeVersion({
          id: 42,
          emulator: "mgba",
          file_size_bytes: 2048,
          file_name: "save-42.srm",
          uploaded_by_us: true,
          device_syncs: [
            { device_id: "d1", device_name: "deck", is_current: true, last_synced_at: "2025-06-14T10:00:00Z" },
          ],
        }),
      ],
    });
    const { getByText, container } = render(<VersionHistoryPanel {...defaultProps()} />);
    fireEvent.click(getByText("Previous Versions"));
    await flushAsync();

    expect(container.textContent).toContain("#42");
    expect(container.textContent).toContain("mgba");
    expect(container.textContent).toContain("2.0 KB");
    expect(container.textContent).toContain("save-42.srm");
    expect(container.textContent).toContain("deck (this device) ✓");
    expect(getByText("Restore")).toBeInTheDocument();
  });

  it("Restore is enabled when not restoring and not offline", async () => {
    vi.mocked(backend.savesListFileVersions).mockResolvedValue({
      status: "ok",
      versions: [makeVersion()],
    });
    // Re-render with offline=false to load versions, then switch to offline.
    // Simpler: load and then assert directly that with isOffline=true, the
    // body is the offline notice, not the version list. So this test is
    // covered by "skips fetch when offline". Here we cover the disabled
    // flag in the restoring path:
    const { getByText } = render(<VersionHistoryPanel {...defaultProps()} />);
    fireEvent.click(getByText("Previous Versions"));
    await flushAsync();
    expect(getByText("Restore")).not.toBeDisabled();
  });

  describe("handleRestore status branches", () => {
    async function expand(props: Partial<React.ComponentProps<typeof VersionHistoryPanel>> = {}) {
      vi.mocked(backend.savesListFileVersions).mockResolvedValue({
        status: "ok",
        versions: [makeVersion({ id: 11 })],
      });
      const onRestored = vi.fn();
      const utils = render(
        <VersionHistoryPanel {...defaultProps({ ...props, onRestored })} />,
      );
      fireEvent.click(utils.getByText("Previous Versions"));
      await flushAsync();
      return { ...utils, onRestored };
    }

    it("status 'ok' toasts, calls onRestored, and collapses", async () => {
      vi.mocked(backend.savesRollbackToVersion).mockResolvedValue({ status: "ok" });
      const { getByText, onRestored, container } = await expand();
      fireEvent.click(getByText("Restore"));
      await flushAsync();
      await flushAsync();
      expect(vi.mocked(toaster.toast)).toHaveBeenCalledWith(
        expect.objectContaining({ title: "RomM Sync", body: expect.stringContaining("Save restored") }),
      );
      expect(onRestored).toHaveBeenCalledTimes(1);
      // Collapsed again — chevron is ▸
      await waitFor(() => expect(container.textContent).toContain("▸"));
    });

    it("status 'conflict_blocked' opens the sync conflict modal with the first conflict", async () => {
      const conflict = {
        type: "sync_conflict" as const,
        rom_id: 1,
        filename: "save.srm",
        server_save_id: 1,
        server_updated_at: "2025-06-15T10:00:00Z",
        server_size: 100,
        local_path: null,
        local_hash: null,
        local_mtime: null,
        local_size: null,
        created_at: "2025-06-15T10:00:00Z",
      };
      vi.mocked(backend.savesRollbackToVersion).mockResolvedValue({
        status: "conflict_blocked",
        conflicts: [conflict],
      });
      const { getByText } = await expand();
      fireEvent.click(getByText("Restore"));
      await flushAsync();
      await flushAsync();
      expect(vi.mocked(showSyncConflictModal)).toHaveBeenCalledWith(conflict);
      expect(vi.mocked(toaster.toast)).toHaveBeenCalledWith(
        expect.objectContaining({ body: "Resolve the conflict, then try again" }),
      );
    });

    it("status 'conflict_blocked' with empty conflicts skips the modal but still toasts", async () => {
      vi.mocked(backend.savesRollbackToVersion).mockResolvedValue({
        status: "conflict_blocked",
        conflicts: [],
      });
      const { getByText } = await expand();
      fireEvent.click(getByText("Restore"));
      await flushAsync();
      await flushAsync();
      expect(vi.mocked(showSyncConflictModal)).not.toHaveBeenCalled();
      expect(vi.mocked(toaster.toast)).toHaveBeenCalledWith(
        expect.objectContaining({ body: "Resolve the conflict, then try again" }),
      );
    });

    it("status 'preflight_failed' toasts the first error detail", async () => {
      vi.mocked(backend.savesRollbackToVersion).mockResolvedValue({
        status: "preflight_failed",
        errors: ["upload failed: timeout"],
      });
      const { getByText } = await expand();
      fireEvent.click(getByText("Restore"));
      await flushAsync();
      await flushAsync();
      expect(vi.mocked(toaster.toast)).toHaveBeenCalledWith(
        expect.objectContaining({ body: "Sync failed before restore: upload failed: timeout" }),
      );
    });

    it("status 'preflight_failed' with empty errors falls back to 'preflight error'", async () => {
      vi.mocked(backend.savesRollbackToVersion).mockResolvedValue({
        status: "preflight_failed",
        errors: [],
      });
      const { getByText } = await expand();
      fireEvent.click(getByText("Restore"));
      await flushAsync();
      await flushAsync();
      expect(vi.mocked(toaster.toast)).toHaveBeenCalledWith(
        expect.objectContaining({ body: "Sync failed before restore: preflight error" }),
      );
    });

    it("status 'put_failed' toasts the local-success warning, collapses, and calls onRestored", async () => {
      vi.mocked(backend.savesRollbackToVersion).mockResolvedValue({
        status: "put_failed",
        error: "503",
      });
      const { getByText, onRestored } = await expand();
      fireEvent.click(getByText("Restore"));
      await flushAsync();
      await flushAsync();
      expect(vi.mocked(toaster.toast)).toHaveBeenCalledWith(
        expect.objectContaining({ body: expect.stringContaining("Restored locally") }),
      );
      expect(onRestored).toHaveBeenCalledTimes(1);
    });

    it("status 'rom_not_installed' toasts the reinstall prompt", async () => {
      vi.mocked(backend.savesRollbackToVersion).mockResolvedValue({
        status: "rom_not_installed",
      });
      const { getByText, onRestored } = await expand();
      fireEvent.click(getByText("Restore"));
      await flushAsync();
      await flushAsync();
      expect(vi.mocked(toaster.toast)).toHaveBeenCalledWith(
        expect.objectContaining({ body: "ROM is no longer installed locally. Reinstall and try again." }),
      );
      expect(onRestored).not.toHaveBeenCalled();
    });

    it("status 'version_deleted' toasts the version-gone message", async () => {
      vi.mocked(backend.savesRollbackToVersion).mockResolvedValue({
        status: "version_deleted",
      });
      const { getByText } = await expand();
      fireEvent.click(getByText("Restore"));
      await flushAsync();
      await flushAsync();
      expect(vi.mocked(toaster.toast)).toHaveBeenCalledWith(
        expect.objectContaining({ body: "This version no longer exists on the server" }),
      );
    });

    it("status 'server_unreachable' toasts the connection-prompt message", async () => {
      vi.mocked(backend.savesRollbackToVersion).mockResolvedValue({
        status: "server_unreachable",
        error: "ECONNREFUSED",
      });
      const { getByText } = await expand();
      fireEvent.click(getByText("Restore"));
      await flushAsync();
      await flushAsync();
      expect(vi.mocked(toaster.toast)).toHaveBeenCalledWith(
        expect.objectContaining({ body: "Couldn't reach RomM. Check your connection and try again." }),
      );
    });

    it("status 'unsupported' toasts the version requirement", async () => {
      vi.mocked(backend.savesRollbackToVersion).mockResolvedValue({
        status: "unsupported",
      });
      const { getByText } = await expand();
      fireEvent.click(getByText("Restore"));
      await flushAsync();
      await flushAsync();
      expect(vi.mocked(toaster.toast)).toHaveBeenCalledWith(
        expect.objectContaining({ body: "Version history requires RomM 4.7+" }),
      );
    });

    it("swallows thrown errors from the rollback call (logged via debugLog, no toast)", async () => {
      vi.mocked(backend.savesRollbackToVersion).mockRejectedValue(new Error("boom"));
      const { getByText } = await expand();
      const initialToasts = vi.mocked(toaster.toast).mock.calls.length;
      fireEvent.click(getByText("Restore"));
      await flushAsync();
      await flushAsync();
      // No new toaster call from the rollback path itself (debugLog only)
      expect(vi.mocked(toaster.toast).mock.calls.length).toBe(initialToasts);
    });
  });

  it("Restore button is disabled while a restore is in flight", async () => {
    vi.mocked(backend.savesListFileVersions).mockResolvedValue({
      status: "ok",
      versions: [makeVersion()],
    });
    let resolveRestore: (v: RollbackStatus) => void = () => undefined;
    vi.mocked(backend.savesRollbackToVersion).mockImplementation(
      () => new Promise((res) => { resolveRestore = res; }),
    );
    const { getByText } = render(<VersionHistoryPanel {...defaultProps()} />);
    fireEvent.click(getByText("Previous Versions"));
    await flushAsync();
    fireEvent.click(getByText("Restore"));
    await flushAsync();
    expect(getByText("Restoring...")).toBeDisabled();
    resolveRestore({ status: "ok" });
    await flushAsync();
  });
});
