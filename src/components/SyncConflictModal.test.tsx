// CATCH-REJECTION ASSERTION RULE:
// SyncConflictModalHost.handleResolve outer try/catch has 3 side effects —
// setErrorMessage, logError, setIsLoading(false). All three are asserted in
// the "throw" branch test below.
//
// SyncConflictModal.handleResolve's inline `.catch(() => {})` is the
// documented truly-ignored boundary catch (see the source comment "onResolve
// owns its own error handling; swallow rejections at the event-handler
// boundary so React doesn't see an unhandled promise"). Per the
// CATCH-REJECTION rule in CLAUDE.md, truly-ignored catches with no
// observable side effect can stay assertion-free. We still cover both legs
// (onResolve resolves vs. rejects) to keep the call site exercised.

import { describe, it, expect, beforeEach, vi } from "vitest";
import { render, fireEvent, act } from "@testing-library/react";
import { createElement, type ReactElement } from "react";
import { showModal } from "@decky/ui";
import { showSyncConflictModal } from "./SyncConflictModal";
import * as backend from "../api/backend";
import type { SyncConflict } from "../types";

// Per-file mock so we can capture the closeModal prop the host passes to
// ModalRoot and assert the isLoading-suppresses-outside-close behavior.
type ModalCloseFn = (() => void) | undefined;
const capturedModalCloseFns: ModalCloseFn[] = [];

vi.mock("@decky/ui", () => {
  type AnyProps = Record<string, unknown> & { children?: unknown };
  return {
    ModalRoot: (p: AnyProps & { closeModal?: () => void }) => {
      capturedModalCloseFns.push(p.closeModal);
      return createElement("div", { "data-testid": "modal-root" }, p.children as never);
    },
    DialogButton: ({ children, onClick, disabled }: AnyProps & { disabled?: boolean }) =>
      createElement("button", { onClick, disabled }, children as never),
    showModal: vi.fn(),
  };
});

function buttonByText(container: HTMLElement, text: string): HTMLButtonElement {
  const btn = Array.from(container.querySelectorAll("button")).find(
    (b) => b.textContent === text,
  );
  if (!btn) throw new Error(`button "${text}" not found`);
  return btn as HTMLButtonElement;
}

function makeConflict(overrides: Partial<SyncConflict> = {}): SyncConflict {
  return {
    type: "sync_conflict",
    rom_id: 42,
    filename: "savefile.srm",
    server_save_id: 9,
    server_updated_at: "2026-05-01T10:00:00Z",
    server_size: 2048,
    local_path: "/saves/savefile.srm",
    local_hash: "abc",
    local_mtime: "2026-05-01T09:00:00Z",
    local_size: 1024,
    created_at: "2026-05-01T11:00:00Z",
    ...overrides,
  };
}

function lastShownElement(): ReactElement {
  const calls = vi.mocked(showModal).mock.calls;
  const el = calls[calls.length - 1]?.[0] as ReactElement | undefined;
  if (!el) throw new Error("showModal was not called");
  return el;
}

// Flush microtasks so React state updates from awaited callables settle.
async function flushAsync(): Promise<void> {
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
  });
}

describe("SyncConflictModal", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    capturedModalCloseFns.length = 0;
    // Default: resolveSyncConflict resolves to success so each test only
    // overrides the branch it is interested in.
    vi.mocked(backend.resolveSyncConflict).mockResolvedValue({ success: true });
  });

  // ---------------------------------------------------------------------------
  // Layer A — formatBytes helper (covered indirectly via rendered output).
  // ---------------------------------------------------------------------------
  describe("formatBytes (via rendered output)", () => {
    it("renders 'unknown' when local_size is null", () => {
      void showSyncConflictModal(makeConflict({ local_size: null }));
      const { container } = render(lastShownElement());
      // "Your local save" block carries the bytes string.
      expect(container.textContent).toContain("unknown");
    });

    it("renders 'unknown' when server_size is 0", () => {
      void showSyncConflictModal(makeConflict({ server_size: 0, local_size: 100 }));
      const { container } = render(lastShownElement());
      expect(container.textContent).toContain("unknown");
    });

    it("renders bytes as 'X.X B' below 1024", () => {
      void showSyncConflictModal(makeConflict({ local_size: 500, server_size: 1023 }));
      const { container } = render(lastShownElement());
      expect(container.textContent).toContain("500.0 B");
      expect(container.textContent).toContain("1023.0 B");
    });

    it("renders bytes as 'X.X KB' for exact 1024", () => {
      void showSyncConflictModal(makeConflict({ local_size: 1024 }));
      const { container } = render(lastShownElement());
      expect(container.textContent).toContain("1.0 KB");
    });

    it("renders bytes as 'X.X MB' above ~1MB", () => {
      void showSyncConflictModal(makeConflict({ local_size: 5 * 1024 * 1024 }));
      const { container } = render(lastShownElement());
      expect(container.textContent).toContain("5.0 MB");
    });
  });

  // ---------------------------------------------------------------------------
  // Layer B — SyncConflictModal presentational FC (rendered via the host).
  // We drive it via the host so we exercise the same wiring users hit; the
  // disabled-button + isLoading flow is covered by loading-branch tests in
  // Layer C below.
  // ---------------------------------------------------------------------------
  describe("SyncConflictModal — presentational", () => {
    it("renders filename, both save blocks, formatted bytes and timestamps", () => {
      const conflict = makeConflict({
        filename: "game.srm",
        server_save_id: 77,
        local_size: 2048,
        server_size: 4096,
      });
      void showSyncConflictModal(conflict);
      const { container } = render(lastShownElement());

      expect(container.textContent).toContain("Save conflict for game.srm");
      expect(container.textContent).toContain("Your local save");
      expect(container.textContent).toContain("Server save (id=77)");
      expect(container.textContent).toContain("2.0 KB");
      expect(container.textContent).toContain("4.0 KB");
      // formatTimestamp is the real util — we don't assert its exact output
      // (locale-dependent), only that the field is non-empty after "modified".
      expect(container.textContent).toMatch(/modified \S/);
      expect(container.textContent).toMatch(/uploaded \S/);
    });

    it("does NOT render the error block when errorMessage is null (default)", () => {
      void showSyncConflictModal(makeConflict());
      const { container } = render(lastShownElement());
      // Initial render before any resolveSyncConflict call → no error.
      expect(container.textContent).not.toContain("Failed to resolve conflict");
    });
  });

  // ---------------------------------------------------------------------------
  // Layer C — SyncConflictModalHost stateful wrapper.
  // ---------------------------------------------------------------------------
  describe("SyncConflictModalHost — handleResolve success branch", () => {
    it("Keep Local: success → resolves promise with 'keep_local'", async () => {
      vi.mocked(backend.resolveSyncConflict).mockResolvedValue({ success: true });
      const conflict = makeConflict();
      const promise = showSyncConflictModal(conflict);
      const { container } = render(lastShownElement());

      await act(async () => {
        fireEvent.click(buttonByText(container, "Keep Local"));
      });
      await flushAsync();

      expect(backend.resolveSyncConflict).toHaveBeenCalledWith(
        conflict.rom_id,
        conflict.filename,
        conflict.server_save_id,
        "keep_local",
      );
      await expect(promise).resolves.toBe("keep_local");
    });

    it("Use Server: success → resolves promise with 'use_server'", async () => {
      vi.mocked(backend.resolveSyncConflict).mockResolvedValue({ success: true });
      const conflict = makeConflict();
      const promise = showSyncConflictModal(conflict);
      const { container } = render(lastShownElement());

      await act(async () => {
        fireEvent.click(buttonByText(container, "Use Server"));
      });
      await flushAsync();

      expect(backend.resolveSyncConflict).toHaveBeenCalledWith(
        conflict.rom_id,
        conflict.filename,
        conflict.server_save_id,
        "use_server",
      );
      await expect(promise).resolves.toBe("use_server");
    });
  });

  describe("SyncConflictModalHost — handleResolve stale_conflict branch", () => {
    it("sets the stale-specific errorMessage, logs 'stale:', does NOT resolve promise", async () => {
      const logSpy = vi
        .spyOn(backend, "logError")
        .mockImplementation(() => {});
      vi.mocked(backend.resolveSyncConflict).mockResolvedValue({
        success: false,
        error_code: "stale_conflict",
        message: "out of date",
      });

      const conflict = makeConflict({ rom_id: 7, filename: "stale.srm" });
      const resolved = vi.fn();
      const promise = showSyncConflictModal(conflict);
      void promise.then(resolved);
      const { container } = render(lastShownElement());

      await act(async () => {
        fireEvent.click(buttonByText(container, "Keep Local"));
      });
      await flushAsync();

      expect(logSpy).toHaveBeenCalledWith(
        expect.stringContaining("stale: out of date"),
      );
      expect(logSpy).toHaveBeenCalledWith(
        expect.stringContaining("resolveSyncConflict(7, stale.srm, keep_local)"),
      );
      // Stale message is set into the host's error state and re-rendered.
      expect(container.textContent).toContain(
        "The server save has been updated by another device",
      );
      // Buttons re-enabled (isLoading flipped back to false).
      expect(buttonByText(container, "Keep Local").disabled).toBe(false);
      // Promise has NOT settled.
      expect(resolved).not.toHaveBeenCalled();

      logSpy.mockRestore();
    });

    it("falls back to empty stale message when result.message is missing", async () => {
      const logSpy = vi
        .spyOn(backend, "logError")
        .mockImplementation(() => {});
      vi.mocked(backend.resolveSyncConflict).mockResolvedValue({
        success: false,
        error_code: "stale_conflict",
      });

      const conflict = makeConflict();
      void showSyncConflictModal(conflict);
      const { container } = render(lastShownElement());

      await act(async () => {
        fireEvent.click(buttonByText(container, "Use Server"));
      });
      await flushAsync();

      // The "stale: " segment is present even with no trailing message.
      expect(logSpy).toHaveBeenCalledWith(expect.stringContaining("stale: "));
      logSpy.mockRestore();
    });
  });

  describe("SyncConflictModalHost — handleResolve generic-failure branch", () => {
    it("sets errorMessage to result.message, logs 'failed: <msg>', does NOT resolve", async () => {
      const logSpy = vi
        .spyOn(backend, "logError")
        .mockImplementation(() => {});
      vi.mocked(backend.resolveSyncConflict).mockResolvedValue({
        success: false,
        message: "permission denied",
      });

      const conflict = makeConflict({ rom_id: 11, filename: "x.srm" });
      const resolved = vi.fn();
      const promise = showSyncConflictModal(conflict);
      void promise.then(resolved);
      const { container } = render(lastShownElement());

      await act(async () => {
        fireEvent.click(buttonByText(container, "Keep Local"));
      });
      await flushAsync();

      expect(logSpy).toHaveBeenCalledWith(
        expect.stringContaining("failed: permission denied"),
      );
      expect(container.textContent).toContain("permission denied");
      expect(buttonByText(container, "Keep Local").disabled).toBe(false);
      expect(resolved).not.toHaveBeenCalled();

      logSpy.mockRestore();
    });

    it("falls back to 'Failed to resolve conflict' when result.message is missing", async () => {
      const logSpy = vi
        .spyOn(backend, "logError")
        .mockImplementation(() => {});
      vi.mocked(backend.resolveSyncConflict).mockResolvedValue({
        success: false,
      });

      void showSyncConflictModal(makeConflict());
      const { container } = render(lastShownElement());

      await act(async () => {
        fireEvent.click(buttonByText(container, "Keep Local"));
      });
      await flushAsync();

      expect(logSpy).toHaveBeenCalledWith(
        expect.stringContaining("failed: Failed to resolve conflict"),
      );
      expect(container.textContent).toContain("Failed to resolve conflict");
      logSpy.mockRestore();
    });
  });

  describe("SyncConflictModalHost — handleResolve throw branch", () => {
    it("Error rejection: sets errorMessage to err.message, logs 'threw:', isLoading flips back", async () => {
      const logSpy = vi
        .spyOn(backend, "logError")
        .mockImplementation(() => {});
      vi.mocked(backend.resolveSyncConflict).mockRejectedValue(new Error("network boom"));

      const conflict = makeConflict({ rom_id: 3, filename: "boom.srm" });
      const resolved = vi.fn();
      const promise = showSyncConflictModal(conflict);
      void promise.then(resolved);
      const { container } = render(lastShownElement());

      await act(async () => {
        fireEvent.click(buttonByText(container, "Keep Local"));
      });
      await flushAsync();

      expect(logSpy).toHaveBeenCalledWith(
        expect.stringContaining("threw: network boom"),
      );
      expect(container.textContent).toContain("network boom");
      expect(buttonByText(container, "Keep Local").disabled).toBe(false);
      expect(resolved).not.toHaveBeenCalled();

      logSpy.mockRestore();
    });

    it("non-Error rejection: uses String(e); empty string falls back to 'Failed to resolve conflict'", async () => {
      const logSpy = vi
        .spyOn(backend, "logError")
        .mockImplementation(() => {});
      // Reject with an empty string — String("") === "" which falls back via
      // the `msg || "Failed to resolve conflict"` ternary.
      vi.mocked(backend.resolveSyncConflict).mockRejectedValue("");

      void showSyncConflictModal(makeConflict());
      const { container } = render(lastShownElement());

      await act(async () => {
        fireEvent.click(buttonByText(container, "Use Server"));
      });
      await flushAsync();

      expect(logSpy).toHaveBeenCalledWith(expect.stringContaining("threw: "));
      expect(container.textContent).toContain("Failed to resolve conflict");
      logSpy.mockRestore();
    });

    it("non-Error rejection with non-empty value: uses String(e) verbatim", async () => {
      const logSpy = vi
        .spyOn(backend, "logError")
        .mockImplementation(() => {});
      vi.mocked(backend.resolveSyncConflict).mockRejectedValue("bare string error");

      void showSyncConflictModal(makeConflict());
      const { container } = render(lastShownElement());

      await act(async () => {
        fireEvent.click(buttonByText(container, "Keep Local"));
      });
      await flushAsync();

      expect(logSpy).toHaveBeenCalledWith(
        expect.stringContaining("threw: bare string error"),
      );
      expect(container.textContent).toContain("bare string error");
      logSpy.mockRestore();
    });
  });

  describe("SyncConflictModalHost — isLoading wiring", () => {
    it("disables all three buttons while resolveSyncConflict is in flight", async () => {
      // Keep the callable pending so isLoading stays true mid-test.
      let resolveCallable: (v: { success: boolean }) => void = () => {};
      vi.mocked(backend.resolveSyncConflict).mockImplementation(
        () => new Promise((res) => {
          resolveCallable = res;
        }),
      );

      void showSyncConflictModal(makeConflict());
      const { container } = render(lastShownElement());

      await act(async () => {
        fireEvent.click(buttonByText(container, "Keep Local"));
      });

      expect(buttonByText(container, "Keep Local").disabled).toBe(true);
      expect(buttonByText(container, "Use Server").disabled).toBe(true);
      expect(buttonByText(container, "Cancel").disabled).toBe(true);

      // Settle the in-flight call so React doesn't hold an open promise.
      await act(async () => {
        resolveCallable({ success: true });
      });
      await flushAsync();
    });

    it("ModalRoot.closeModal is undefined while loading (suppresses outside-click close)", async () => {
      let resolveCallable: (v: { success: boolean }) => void = () => {};
      vi.mocked(backend.resolveSyncConflict).mockImplementation(
        () => new Promise((res) => {
          resolveCallable = res;
        }),
      );

      void showSyncConflictModal(makeConflict());
      render(lastShownElement());

      // Initial render: not loading → closeModal === handleCancel (defined).
      expect(typeof capturedModalCloseFns[0]).toBe("function");

      await act(async () => {
        fireEvent.click(
          buttonByText(document.body as HTMLElement, "Keep Local"),
        );
      });

      // After click, React re-rendered with isLoading=true → ModalRoot's
      // closeModal prop is now undefined.
      const last = capturedModalCloseFns[capturedModalCloseFns.length - 1];
      expect(last).toBeUndefined();

      await act(async () => {
        resolveCallable({ success: true });
      });
      await flushAsync();
    });
  });

  describe("SyncConflictModalHost — handleCancel", () => {
    it("not loading: resolves promise with 'cancel'", async () => {
      const conflict = makeConflict();
      const promise = showSyncConflictModal(conflict);
      const { container } = render(lastShownElement());

      await act(async () => {
        fireEvent.click(buttonByText(container, "Cancel"));
      });

      await expect(promise).resolves.toBe("cancel");
      // resolveSyncConflict was never called — pure UI close.
      expect(backend.resolveSyncConflict).not.toHaveBeenCalled();
    });

    it("loading: Cancel button is disabled (no-op via DOM is unreachable, ModalRoot close is undefined)", async () => {
      let resolveCallable: (v: { success: boolean }) => void = () => {};
      vi.mocked(backend.resolveSyncConflict).mockImplementation(
        () => new Promise((res) => {
          resolveCallable = res;
        }),
      );

      const resolved = vi.fn();
      const promise = showSyncConflictModal(makeConflict());
      void promise.then(resolved);
      const { container } = render(lastShownElement());

      await act(async () => {
        fireEvent.click(buttonByText(container, "Keep Local"));
      });

      // Cancel button is disabled — happy-dom won't fire onClick on a disabled
      // button, so clicking is a no-op without us needing the `if (isLoading) return`
      // guard. ModalRoot's closeModal is undefined for the same reason, which
      // is the other half of the loading-guard contract (asserted above).
      const cancelBtn = buttonByText(container, "Cancel");
      expect(cancelBtn.disabled).toBe(true);
      fireEvent.click(cancelBtn);
      expect(resolved).not.toHaveBeenCalled();

      await act(async () => {
        resolveCallable({ success: true });
      });
      await flushAsync();
    });
  });

  describe("showSyncConflictModal — Promise wrapper", () => {
    it("returns a Promise that resolves to 'keep_local' on success", async () => {
      vi.mocked(backend.resolveSyncConflict).mockResolvedValue({ success: true });
      const promise = showSyncConflictModal(makeConflict());
      const { container } = render(lastShownElement());

      await act(async () => {
        fireEvent.click(buttonByText(container, "Keep Local"));
      });
      await expect(promise).resolves.toBe("keep_local");
    });

    it("returns a Promise that resolves to 'use_server' on success", async () => {
      vi.mocked(backend.resolveSyncConflict).mockResolvedValue({ success: true });
      const promise = showSyncConflictModal(makeConflict());
      const { container } = render(lastShownElement());

      await act(async () => {
        fireEvent.click(buttonByText(container, "Use Server"));
      });
      await expect(promise).resolves.toBe("use_server");
    });

    it("returns a Promise that resolves to 'cancel' on Cancel click", async () => {
      const promise = showSyncConflictModal(makeConflict());
      const { container } = render(lastShownElement());

      await act(async () => {
        fireEvent.click(buttonByText(container, "Cancel"));
      });
      await expect(promise).resolves.toBe("cancel");
    });
  });
});
