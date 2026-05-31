import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { render } from "@testing-library/react";
import { renderSaveFileRow, infoRow } from "./SaveFileRow";
import type { SaveFileStatus, SyncConflict } from "../../types";

function makeFile(overrides: Partial<SaveFileStatus> = {}): SaveFileStatus {
  return {
    filename: "save.srm",
    local_path: null,
    local_hash: null,
    local_mtime: null,
    local_size: null,
    server_save_id: null,
    server_file_name: null,
    server_emulator: null,
    server_updated_at: null,
    server_size: null,
    last_sync_at: null,
    status: "synced",
    ...overrides,
  };
}

function makeConflict(overrides: Partial<SyncConflict> = {}): SyncConflict {
  return {
    type: "sync_conflict",
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
    ...overrides,
  };
}

describe("infoRow", () => {
  it("returns null when value is null", () => {
    expect(infoRow("k", "Label:", null)).toBeNull();
  });

  it("returns null when value is an empty string", () => {
    expect(infoRow("k", "Label:", "")).toBeNull();
  });

  it("renders a row when value is a non-empty string", () => {
    const el = infoRow("k", "Last:", "hello");
    expect(el).not.toBeNull();
    const { container } = render(<div>{el}</div>);
    expect(container.textContent).toContain("Last:");
    expect(container.textContent).toContain("hello");
  });
});

describe("renderSaveFileRow", () => {
  // Pin time for deterministic relative formatting
  beforeEach(() => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2025-06-15T12:00:00Z"));
  });
  afterEach(() => vi.useRealTimers());

  it("renders filename", () => {
    const { container } = render(<div>{renderSaveFileRow(makeFile(), undefined, null)}</div>);
    expect(container.textContent).toContain("save.srm");
  });

  it("renders 'Synced' status badge for status 'synced'", () => {
    const { container } = render(<div>{renderSaveFileRow(makeFile({ status: "synced" }), undefined, null)}</div>);
    expect(container.querySelector(".romm-save-status-label")?.textContent).toBe("Synced");
  });

  it("renders 'Local changes' badge for status 'upload'", () => {
    const { container } = render(<div>{renderSaveFileRow(makeFile({ status: "upload" }), undefined, null)}</div>);
    expect(container.querySelector(".romm-save-status-label")?.textContent).toBe("Local changes");
  });

  it("renders 'Server newer' badge for status 'download'", () => {
    const { container } = render(<div>{renderSaveFileRow(makeFile({ status: "download" }), undefined, null)}</div>);
    expect(container.querySelector(".romm-save-status-label")?.textContent).toBe("Server newer");
  });

  it("renders 'Conflict' badge for status 'conflict'", () => {
    const { container } = render(<div>{renderSaveFileRow(makeFile({ status: "conflict" }), undefined, null)}</div>);
    expect(container.querySelector(".romm-save-status-label")?.textContent).toBe("Conflict");
  });

  it("renders 'Status unknown' badge for status 'unknown'", () => {
    const { container } = render(<div>{renderSaveFileRow(makeFile({ status: "unknown" }), undefined, null)}</div>);
    expect(container.querySelector(".romm-save-status-label")?.textContent).toBe("Status unknown");
  });

  it("renders 'Not synced' badge when status is unrecognized and last_sync_at is null", () => {
    const { container } = render(
      <div>
        {renderSaveFileRow(makeFile({ status: "weird" as unknown as SaveFileStatus["status"] }), undefined, null)}
      </div>,
    );
    expect(container.querySelector(".romm-save-status-label")?.textContent).toBe("Not synced");
  });

  it("shows size in the header when local_size is set", () => {
    const { container } = render(<div>{renderSaveFileRow(makeFile({ local_size: 2048 }), undefined, null)}</div>);
    expect(container.textContent).toContain("2.0 KB");
  });

  it("omits size when local_size is null", () => {
    const { container } = render(<div>{renderSaveFileRow(makeFile({ local_size: null }), undefined, null)}</div>);
    expect(container.textContent).not.toContain("KB");
    expect(container.textContent).not.toContain(" B");
  });

  it("shows the conflict banner when status === 'conflict'", () => {
    const { container } = render(<div>{renderSaveFileRow(makeFile({ status: "conflict" }), undefined, null)}</div>);
    expect(container.textContent).toContain("Conflict detected — resolve from the sync action");
  });

  it("shows the conflict banner when a conflict arg is passed (even with non-conflict status)", () => {
    const { container } = render(<div>{renderSaveFileRow(makeFile({ status: "synced" }), makeConflict(), null)}</div>);
    expect(container.textContent).toContain("Conflict detected — resolve from the sync action");
  });

  it("hides the conflict banner when no conflict and status is not 'conflict'", () => {
    const { container } = render(<div>{renderSaveFileRow(makeFile({ status: "synced" }), undefined, null)}</div>);
    expect(container.textContent).not.toContain("Conflict detected — resolve from the sync action");
  });

  it("renders the last-synced info row with 'Never' when neither check nor file timestamp is set", () => {
    const { container } = render(<div>{renderSaveFileRow(makeFile({ last_sync_at: null }), undefined, null)}</div>);
    expect(container.textContent).toContain("Last synced:");
    expect(container.textContent).toContain("Never");
  });

  it("renders relative time in the last-synced row when last_sync_check_at is set", () => {
    const { container } = render(
      <div>
        {renderSaveFileRow(makeFile({ status: "synced", last_sync_at: null }), undefined, "2025-06-15T11:30:00Z")}
      </div>,
    );
    expect(container.textContent).toContain("30m ago");
  });

  it("renders the last-updated info row when server_updated_at is set", () => {
    const { container } = render(
      <div>{renderSaveFileRow(makeFile({ server_updated_at: "2025-06-15T10:00:00Z" }), undefined, null)}</div>,
    );
    expect(container.textContent).toContain("Last updated:");
  });

  it("skips the last-updated row when server_updated_at is null", () => {
    const { container } = render(
      <div>{renderSaveFileRow(makeFile({ server_updated_at: null }), undefined, null)}</div>,
    );
    expect(container.textContent).not.toContain("Last updated:");
  });

  it("renders the server save sub-block (id-only) when server_save_id is set", () => {
    const { container } = render(<div>{renderSaveFileRow(makeFile({ server_save_id: 42 }), undefined, null)}</div>);
    expect(container.textContent).toContain("Server save:");
    expect(container.textContent).toContain("#42");
  });

  it("renders the server save sub-block (id + emulator) when both are set", () => {
    const { container } = render(
      <div>
        {renderSaveFileRow(makeFile({ server_save_id: 42, server_emulator: "retroarch-mgba" }), undefined, null)}
      </div>,
    );
    expect(container.textContent).toContain("#42");
    expect(container.textContent).toContain("retroarch-mgba");
  });

  it("renders the server filename line when server_file_name is set", () => {
    const { container } = render(
      <div>
        {renderSaveFileRow(makeFile({ server_save_id: 42, server_file_name: "remote_save.srm" }), undefined, null)}
      </div>,
    );
    expect(container.textContent).toContain("remote_save.srm");
  });

  it("skips the server save sub-block when server_save_id is null", () => {
    const { container } = render(<div>{renderSaveFileRow(makeFile({ server_save_id: null }), undefined, null)}</div>);
    expect(container.textContent).not.toContain("Server save:");
  });

  it("renders the local-path row when local_path is set", () => {
    const { container } = render(
      <div>{renderSaveFileRow(makeFile({ local_path: "/data/save.srm" }), undefined, null)}</div>,
    );
    expect(container.textContent).toContain("Local path:");
    expect(container.textContent).toContain("/data/save.srm");
  });

  it("skips the local-path row when local_path is null", () => {
    const { container } = render(<div>{renderSaveFileRow(makeFile({ local_path: null }), undefined, null)}</div>);
    expect(container.textContent).not.toContain("Local path:");
  });

  it("appends '(this device) ✓' attribution segment when uploaded_by_us is true", () => {
    const { container } = render(
      <div>
        {renderSaveFileRow(
          makeFile({
            status: "synced",
            uploaded_by_us: true,
            device_syncs: [
              { device_id: "d1", device_name: "deck", is_current: true, last_synced_at: "2025-06-15T11:00:00Z" },
            ],
          }),
          undefined,
          "2025-06-15T11:30:00Z",
        )}
      </div>,
    );
    expect(container.textContent).toContain("deck (this device) ✓");
  });

  it("appends '(not this device) ✓' attribution segment when uploaded_by_us is false", () => {
    const { container } = render(
      <div>
        {renderSaveFileRow(
          makeFile({
            status: "synced",
            uploaded_by_us: false,
            device_syncs: [
              { device_id: "d1", device_name: "deck", is_current: true, last_synced_at: "2025-06-15T11:00:00Z" },
            ],
          }),
          undefined,
          "2025-06-15T11:30:00Z",
        )}
      </div>,
    );
    expect(container.textContent).toContain("(not this device) ✓");
  });

  it("appends 'Newer version available on server' when is_current is false", () => {
    const { container } = render(
      <div>
        {renderSaveFileRow(makeFile({ status: "synced", is_current: false }), undefined, "2025-06-15T11:30:00Z")}
      </div>,
    );
    expect(container.textContent).toContain("Newer version available on server");
  });
});
