import { describe, it, expect, vi } from "vitest";
import {
  handleGlobalDownloadFailure,
  handleButtonDownloadFailure,
  type DownloadStoreLike,
  type ToasterLike,
} from "./downloadFailure";
import type { DownloadFailedEvent, DownloadItem } from "../types";

function makeEvent(overrides: Partial<DownloadFailedEvent> = {}): DownloadFailedEvent {
  return {
    rom_id: 42,
    rom_name: "Super Mario 64",
    platform_name: "Nintendo 64",
    error_message: "disk full",
    ...overrides,
  };
}

describe("handleGlobalDownloadFailure", () => {
  it("toasts with the formatted failure message and updates the store entry", () => {
    const prior: DownloadItem = {
      rom_id: 42,
      rom_name: "Super Mario 64",
      platform_name: "Nintendo 64",
      file_name: "sm64.z64",
      status: "downloading",
      progress: 73,
      bytes_downloaded: 730,
      total_bytes: 1000,
    };
    const store: DownloadStoreLike = {
      getDownloadState: vi.fn(() => [prior]),
      updateDownload: vi.fn(),
    };
    const toast: ToasterLike = { toast: vi.fn() };

    handleGlobalDownloadFailure(makeEvent(), store, toast);

    expect(store.updateDownload).toHaveBeenCalledOnce();
    expect(store.updateDownload).toHaveBeenCalledWith({
      rom_id: 42,
      rom_name: "Super Mario 64",
      platform_name: "Nintendo 64",
      file_name: "sm64.z64",
      status: "failed",
      progress: 73,
      bytes_downloaded: 730,
      total_bytes: 1000,
      error: "disk full",
    });
    expect(toast.toast).toHaveBeenCalledOnce();
    expect(toast.toast).toHaveBeenCalledWith({
      title: "RomM Sync",
      body: "Download failed: Super Mario 64 — disk full",
    });
  });

  it("zero-fills progress fields when no prior entry exists", () => {
    const store: DownloadStoreLike = {
      getDownloadState: vi.fn(() => []),
      updateDownload: vi.fn(),
    };
    const toast: ToasterLike = { toast: vi.fn() };

    handleGlobalDownloadFailure(makeEvent(), store, toast);

    expect(store.updateDownload).toHaveBeenCalledWith({
      rom_id: 42,
      rom_name: "Super Mario 64",
      platform_name: "Nintendo 64",
      file_name: "",
      status: "failed",
      progress: 0,
      bytes_downloaded: 0,
      total_bytes: 0,
      error: "disk full",
    });
  });

  it("includes an empty error_message in the toast verbatim", () => {
    const store: DownloadStoreLike = {
      getDownloadState: vi.fn(() => []),
      updateDownload: vi.fn(),
    };
    const toast: ToasterLike = { toast: vi.fn() };

    handleGlobalDownloadFailure(makeEvent({ error_message: "" }), store, toast);

    expect(toast.toast).toHaveBeenCalledWith({
      title: "RomM Sync",
      body: "Download failed: Super Mario 64 — ",
    });
    const [updatedCall] = (store.updateDownload as unknown as { mock: { calls: [DownloadItem][] } }).mock.calls;
    expect(updatedCall![0].error).toBe("");
  });

  it("ignores entries for other roms when selecting the prior state", () => {
    const otherRom: DownloadItem = {
      rom_id: 99,
      rom_name: "Other",
      platform_name: "Other",
      file_name: "other.bin",
      status: "downloading",
      progress: 50,
      bytes_downloaded: 500,
      total_bytes: 1000,
    };
    const store: DownloadStoreLike = {
      getDownloadState: vi.fn(() => [otherRom]),
      updateDownload: vi.fn(),
    };
    const toast: ToasterLike = { toast: vi.fn() };

    handleGlobalDownloadFailure(makeEvent(), store, toast);

    const [updatedCall] = (store.updateDownload as unknown as { mock: { calls: [DownloadItem][] } }).mock.calls;
    // Should NOT have pulled file_name / progress from rom 99.
    expect(updatedCall![0].file_name).toBe("");
    expect(updatedCall![0].progress).toBe(0);
  });
});

describe("handleButtonDownloadFailure", () => {
  it("invokes the reset callback when the event matches the button's rom_id", () => {
    const reset = vi.fn();
    handleButtonDownloadFailure(makeEvent({ rom_id: 7 }), 7, reset);
    expect(reset).toHaveBeenCalledOnce();
  });

  it("does nothing when the event targets a different rom_id", () => {
    const reset = vi.fn();
    handleButtonDownloadFailure(makeEvent({ rom_id: 7 }), 8, reset);
    expect(reset).not.toHaveBeenCalled();
  });

  it("does nothing when rom_id is 0 vs a non-zero target", () => {
    const reset = vi.fn();
    handleButtonDownloadFailure(makeEvent({ rom_id: 0 }), 1, reset);
    expect(reset).not.toHaveBeenCalled();
  });

  it("does nothing when romId is null (button has no associated rom yet)", () => {
    const reset = vi.fn();
    handleButtonDownloadFailure(makeEvent({ rom_id: 7 }), null, reset);
    expect(reset).not.toHaveBeenCalled();
  });
});
