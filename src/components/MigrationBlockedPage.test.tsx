// CATCH-REJECTION ASSERTION RULE:
// Every catch block with a setX(...) side effect MUST have its side effect
// asserted in the test. MigrationBlockedPage has 2 catches:
//   - runMigration catch → setMigrateResult("Migration failed")
//   - handleDismiss onOK catch → setMigrateResult("Dismiss failed")
// Both surface through the rendered <Field label={migrateResult} /> — the
// catch tests below assert that label text.

import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { render, fireEvent, act, renderHook } from "@testing-library/react";
import { createElement, type ComponentProps, type ReactElement } from "react";
import { showModal } from "@decky/ui";
import { toaster } from "@decky/api";
import { MigrationBlockedPage, useMigrationStatus } from "./MigrationBlockedPage";
import * as backend from "../api/backend";
import {
  setMigrationStatus,
  clearMigration,
  getMigrationState,
} from "../utils/migrationStore";
import * as migrationStore from "../utils/migrationStore";
import type { MigrationStatus, MigrationResult } from "../api/backend";
// Type-only — vi.mock("./MigrationConflictModal") below replaces the runtime
// impl; the captured-props type stays pinned to the real component.
import type { MigrationConflictModal } from "./MigrationConflictModal";

vi.mock("../api/backend", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api/backend")>();
  return {
    ...actual,
    migrateRetroDeckFiles: vi.fn(),
    dismissRetrodeckMigration: vi.fn(),
  };
});

vi.mock("../utils/scrollHelpers", () => ({ scrollToTop: vi.fn() }));

// showModal is a vi.fn() — it captures the React element but never mounts
// it. So tests read MigrationConflictModal props off the captured element
// via shownModalPropsAt(...). The mock stub below is defensive only (in
// case any future code path actually mounts the modal).
type CapturedConflictModalProps = ComponentProps<typeof MigrationConflictModal>;
vi.mock("./MigrationConflictModal", () => ({
  MigrationConflictModal: () =>
    createElement("div", { "data-testid": "migration-conflict-modal" }),
}));

// Helpers — pull props off the React element passed to showModal.
function lastShownModalProps<T = Record<string, unknown>>(): T | null {
  const calls = vi.mocked(showModal).mock.calls;
  if (calls.length === 0) return null;
  const el = calls[calls.length - 1]?.[0] as ReactElement<T> | undefined;
  return el?.props ?? null;
}

function shownModalPropsAt<T = Record<string, unknown>>(idx: number): T | null {
  const calls = vi.mocked(showModal).mock.calls;
  const el = calls[idx]?.[0] as ReactElement<T> | undefined;
  return el?.props ?? null;
}

const flushAsync = () =>
  act(async () => {
    await Promise.resolve();
    await Promise.resolve();
    await Promise.resolve();
  });

const defaultMigration: MigrationStatus = {
  pending: true,
  old_path: "/old/retrodeck",
  new_path: "/new/retrodeck",
  roms_count: 3,
  bios_count: 2,
  saves_count: 5,
};

describe("MigrationBlockedPage component", () => {
  beforeEach(() => {
    vi.mocked(showModal).mockClear();
    vi.mocked(toaster.toast).mockClear();
    vi.mocked(backend.migrateRetroDeckFiles).mockReset();
    vi.mocked(backend.dismissRetrodeckMigration).mockReset();
    // Reset migrationStore so listener-based hooks aren't carrying leak state.
    clearMigration();
  });

  describe("render", () => {
    it("renders header + migration paths + counts", () => {
      const { container } = render(
        <MigrationBlockedPage migration={defaultMigration} />,
      );
      // PanelSection's `title` prop is forwarded by the global stub as a DOM
      // attribute on <section>, so assert via getAttribute, not textContent.
      const section = container.querySelector("section");
      expect(section?.getAttribute("title")).toBe("RetroDECK Migration Required");
      expect(container.textContent).toContain("RetroDECK location changed");
      expect(container.textContent).toContain("From: /old/retrodeck");
      expect(container.textContent).toContain("To: /new/retrodeck");
      expect(container.textContent).toContain(
        "3 ROM(s), 2 BIOS, 5 save(s) to migrate",
      );
    });

    it("renders 'unknown' / zero counts when migration fields are missing", () => {
      const { container } = render(
        <MigrationBlockedPage migration={{ pending: true }} />,
      );
      expect(container.textContent).toContain("From: unknown");
      expect(container.textContent).toContain("To: unknown");
      expect(container.textContent).toContain(
        "0 ROM(s), 0 BIOS, 0 save(s) to migrate",
      );
    });

    it("shows the dismissal hint footer", () => {
      const { container } = render(
        <MigrationBlockedPage migration={defaultMigration} />,
      );
      expect(container.textContent).toContain(
        "Or revert RetroDECK to its previous location",
      );
    });

    it("renders Migrate Files button labelled 'Migrate Files' by default", () => {
      const { getByText } = render(
        <MigrationBlockedPage migration={defaultMigration} />,
      );
      expect(getByText("Migrate Files")).toBeInTheDocument();
      expect(getByText("Dismiss")).toBeInTheDocument();
    });

    it("does not render the result Field until migrateResult is non-empty", () => {
      const { queryByTestId } = render(
        <MigrationBlockedPage migration={defaultMigration} />,
      );
      expect(queryByTestId("field")).toBeNull();
    });
  });

  describe("runMigration via 'Migrate Files' button", () => {
    it("happy path: success → clearMigration + toaster.toast + Field shows result.message", async () => {
      setMigrationStatus(defaultMigration);
      vi.mocked(backend.migrateRetroDeckFiles).mockResolvedValue({
        success: true,
        message: "Migrated 3 ROMs",
      });
      const { getByText, queryByTestId } = render(
        <MigrationBlockedPage migration={defaultMigration} />,
      );
      await act(async () => {
        fireEvent.click(getByText("Migrate Files"));
        await flushAsync();
      });
      expect(backend.migrateRetroDeckFiles).toHaveBeenCalledWith(null);
      // clearMigration() was invoked → store is { pending: false }.
      expect(getMigrationState()).toEqual({ pending: false });
      expect(toaster.toast).toHaveBeenCalledWith({
        title: "RomM Sync",
        body: "Migrated 3 ROMs",
      });
      // Field now visible with the result message.
      const labelEl = queryByTestId("field-label");
      expect(labelEl?.textContent).toBe("Migrated 3 ROMs");
    });

    it("happy path: success with empty message → toast falls back to 'Migration complete.'", async () => {
      vi.mocked(backend.migrateRetroDeckFiles).mockResolvedValue({
        success: true,
        message: "",
      });
      const { getByText } = render(
        <MigrationBlockedPage migration={defaultMigration} />,
      );
      await act(async () => {
        fireEvent.click(getByText("Migrate Files"));
        await flushAsync();
      });
      expect(toaster.toast).toHaveBeenCalledWith({
        title: "RomM Sync",
        body: "Migration complete.",
      });
    });

    it("success=false → no clearMigration, no toast, but Field still shows the message", async () => {
      setMigrationStatus(defaultMigration);
      vi.mocked(backend.migrateRetroDeckFiles).mockResolvedValue({
        success: false,
        message: "Partial failure",
      });
      const { getByText, queryByTestId } = render(
        <MigrationBlockedPage migration={defaultMigration} />,
      );
      await act(async () => {
        fireEvent.click(getByText("Migrate Files"));
        await flushAsync();
      });
      // Store unchanged (clearMigration not called).
      expect(getMigrationState()).toEqual(defaultMigration);
      expect(toaster.toast).not.toHaveBeenCalled();
      expect(queryByTestId("field-label")?.textContent).toBe("Partial failure");
    });

    it("needs_confirmation → opens MigrationConflictModal with conflict_count + recursive runMigration on onChoice", async () => {
      // First call returns needs_confirmation; second call (recursive) returns success.
      vi.mocked(backend.migrateRetroDeckFiles)
        .mockResolvedValueOnce({
          success: false,
          message: "",
          needs_confirmation: true,
          conflict_count: 4,
        })
        .mockResolvedValueOnce({
          success: true,
          message: "Done after overwrite",
        });
      const { getByText } = render(
        <MigrationBlockedPage migration={defaultMigration} />,
      );
      await act(async () => {
        fireEvent.click(getByText("Migrate Files"));
        await flushAsync();
      });
      // Conflict modal element was passed to showModal. showModal is a
      // vi.fn() — the element is captured, not rendered — so pull conflict
      // count + onChoice off the captured React element's props directly.
      expect(showModal).toHaveBeenCalledTimes(1);
      const conflictModalProps = lastShownModalProps<CapturedConflictModalProps>();
      expect(conflictModalProps?.conflictCount).toBe(4);
      expect(typeof conflictModalProps?.onChoice).toBe("function");
      // Drive the recursive call by invoking onChoice("overwrite").
      await act(async () => {
        conflictModalProps?.onChoice("overwrite");
        await flushAsync();
      });
      // 2 backend invocations total: first null, second "overwrite".
      expect(backend.migrateRetroDeckFiles).toHaveBeenCalledTimes(2);
      expect(backend.migrateRetroDeckFiles).toHaveBeenNthCalledWith(1, null);
      expect(backend.migrateRetroDeckFiles).toHaveBeenNthCalledWith(2, "overwrite");
      // Second call's success path fired the toast.
      expect(toaster.toast).toHaveBeenCalledWith({
        title: "RomM Sync",
        body: "Done after overwrite",
      });
    });

    it("needs_confirmation with undefined conflict_count → modal gets 0", async () => {
      vi.mocked(backend.migrateRetroDeckFiles).mockResolvedValueOnce({
        success: false,
        message: "",
        needs_confirmation: true,
      } as MigrationResult);
      const { getByText } = render(
        <MigrationBlockedPage migration={defaultMigration} />,
      );
      await act(async () => {
        fireEvent.click(getByText("Migrate Files"));
        await flushAsync();
      });
      const props = lastShownModalProps<CapturedConflictModalProps>();
      expect(props?.conflictCount).toBe(0);
    });

    it("catch: migrateRetroDeckFiles rejects → Field shows 'Migration failed'", async () => {
      vi.mocked(backend.migrateRetroDeckFiles).mockRejectedValue(
        new Error("net"),
      );
      const { getByText, queryByTestId } = render(
        <MigrationBlockedPage migration={defaultMigration} />,
      );
      await act(async () => {
        fireEvent.click(getByText("Migrate Files"));
        await flushAsync();
      });
      // CATCH-REJECTION rule: assert the post-catch state, not just the call.
      expect(queryByTestId("field-label")?.textContent).toBe("Migration failed");
      // toast not invoked on rejection.
      expect(toaster.toast).not.toHaveBeenCalled();
    });

    it("'Migrating...' label + both buttons disabled while in flight", async () => {
      let resolveFn: (v: MigrationResult) => void = () => {};
      vi.mocked(backend.migrateRetroDeckFiles).mockReturnValue(
        new Promise<MigrationResult>((res) => {
          resolveFn = res;
        }),
      );
      const { getByText, queryByText } = render(
        <MigrationBlockedPage migration={defaultMigration} />,
      );
      await act(async () => {
        fireEvent.click(getByText("Migrate Files"));
      });
      // While the promise is pending, label flips to "Migrating..." and both
      // buttons disabled.
      expect(queryByText("Migrating...")).not.toBeNull();
      expect(queryByText("Migrate Files")).toBeNull();
      const migratingBtn = queryByText("Migrating...") as HTMLButtonElement;
      const dismissBtn = queryByText("Dismiss") as HTMLButtonElement;
      expect(migratingBtn.disabled).toBe(true);
      expect(dismissBtn.disabled).toBe(true);
      // Resolve so the test cleanly tears down without dangling promises.
      await act(async () => {
        resolveFn({ success: true, message: "done" });
        await flushAsync();
      });
    });

    it("needs_confirmation resets 'Migrating...' label back to 'Migrate Files'", async () => {
      vi.mocked(backend.migrateRetroDeckFiles).mockResolvedValueOnce({
        success: false,
        message: "",
        needs_confirmation: true,
        conflict_count: 1,
      });
      const { getByText, queryByText } = render(
        <MigrationBlockedPage migration={defaultMigration} />,
      );
      await act(async () => {
        fireEvent.click(getByText("Migrate Files"));
        await flushAsync();
      });
      // needs_confirmation branch sets migrating=false BEFORE showModal —
      // so the button label flips back.
      expect(queryByText("Migrate Files")).not.toBeNull();
      expect(queryByText("Migrating...")).toBeNull();
    });
  });

  describe("handleDismiss via 'Dismiss' button", () => {
    it("opens ConfirmModal with the expected title + button text", () => {
      const { getByText } = render(
        <MigrationBlockedPage migration={defaultMigration} />,
      );
      fireEvent.click(getByText("Dismiss"));
      expect(showModal).toHaveBeenCalledTimes(1);
      const props = lastShownModalProps<{
        strTitle?: string;
        strOKButtonText?: string;
        strCancelButtonText?: string;
        strDescription?: string;
      }>();
      expect(props?.strTitle).toBe("Dismiss Migration?");
      expect(props?.strOKButtonText).toBe("Dismiss");
      expect(props?.strCancelButtonText).toBe("Cancel");
      expect(props?.strDescription).toContain(
        "This will accept that some ROMs",
      );
    });

    it("onOK happy path: success → clearMigration + toast 'Migration dismissed.'", async () => {
      setMigrationStatus(defaultMigration);
      vi.mocked(backend.dismissRetrodeckMigration).mockResolvedValue({
        success: true,
      });
      const { getByText } = render(
        <MigrationBlockedPage migration={defaultMigration} />,
      );
      fireEvent.click(getByText("Dismiss"));
      const confirm = lastShownModalProps<{
        onOK?: () => void | Promise<void>;
      }>();
      await act(async () => {
        await confirm?.onOK?.();
      });
      expect(backend.dismissRetrodeckMigration).toHaveBeenCalled();
      expect(getMigrationState()).toEqual({ pending: false });
      expect(toaster.toast).toHaveBeenCalledWith({
        title: "RomM Sync",
        body: "Migration dismissed.",
      });
    });

    it("onOK success=false → no clearMigration, no toast", async () => {
      setMigrationStatus(defaultMigration);
      vi.mocked(backend.dismissRetrodeckMigration).mockResolvedValue({
        success: false,
      });
      const { getByText } = render(
        <MigrationBlockedPage migration={defaultMigration} />,
      );
      fireEvent.click(getByText("Dismiss"));
      const confirm = lastShownModalProps<{
        onOK?: () => void | Promise<void>;
      }>();
      await act(async () => {
        await confirm?.onOK?.();
      });
      expect(getMigrationState()).toEqual(defaultMigration);
      expect(toaster.toast).not.toHaveBeenCalled();
    });

    it("catch: dismissRetrodeckMigration rejects → Field shows 'Dismiss failed'", async () => {
      vi.mocked(backend.dismissRetrodeckMigration).mockRejectedValue(
        new Error("io"),
      );
      const { getByText, queryByTestId } = render(
        <MigrationBlockedPage migration={defaultMigration} />,
      );
      fireEvent.click(getByText("Dismiss"));
      const confirm = lastShownModalProps<{
        onOK?: () => void | Promise<void>;
      }>();
      await act(async () => {
        await confirm?.onOK?.();
      });
      // CATCH-REJECTION rule.
      expect(queryByTestId("field-label")?.textContent).toBe("Dismiss failed");
      expect(toaster.toast).not.toHaveBeenCalled();
    });
  });

  describe("integration — Dismiss button opens the second showModal after a Migrate flow", () => {
    it("first showModal = MigrationConflictModal, second = ConfirmModal", async () => {
      vi.mocked(backend.migrateRetroDeckFiles).mockResolvedValueOnce({
        success: false,
        message: "",
        needs_confirmation: true,
        conflict_count: 1,
      });
      const { getByText } = render(
        <MigrationBlockedPage migration={defaultMigration} />,
      );
      await act(async () => {
        fireEvent.click(getByText("Migrate Files"));
        await flushAsync();
      });
      // First showModal call carries the conflict modal element.
      expect(showModal).toHaveBeenCalledTimes(1);
      expect(shownModalPropsAt<CapturedConflictModalProps>(0)?.conflictCount).toBe(1);
      // Now click Dismiss — second showModal is the ConfirmModal.
      fireEvent.click(getByText("Dismiss"));
      expect(showModal).toHaveBeenCalledTimes(2);
      const confirm = lastShownModalProps<{ strTitle?: string }>();
      expect(confirm?.strTitle).toBe("Dismiss Migration?");
    });
  });
});

describe("useMigrationStatus hook", () => {
  afterEach(() => {
    clearMigration();
  });

  it("returns the current state from getMigrationState() on mount", () => {
    setMigrationStatus({ pending: true, old_path: "/x" });
    const { result } = renderHook(() => useMigrationStatus());
    expect(result.current).toEqual({ pending: true, old_path: "/x" });
  });

  it("updates when setMigrationStatus fires after mount", () => {
    const { result } = renderHook(() => useMigrationStatus());
    expect(result.current).toEqual({ pending: false });
    act(() => {
      setMigrationStatus({ pending: true, roms_count: 9 });
    });
    expect(result.current).toEqual({ pending: true, roms_count: 9 });
  });

  it("updates when clearMigration fires", () => {
    setMigrationStatus({ pending: true, roms_count: 9 });
    const { result } = renderHook(() => useMigrationStatus());
    expect(result.current.pending).toBe(true);
    act(() => {
      clearMigration();
    });
    expect(result.current).toEqual({ pending: false });
  });

  it("unsubscribes on unmount — useEffect cleanup invokes the returned unsubscribe", () => {
    // Spy on onMigrationChange so the test owns the unsubscribe callback.
    // The "updates after mount" tests above cover the subscribe-then-callback
    // path; this test isolates the cleanup invocation. Asserting unsubSpy
    // was called proves the hook returns its unsubscribe from useEffect — a
    // mutation that drops the return fails this test, where the prior
    // `not.toThrow()` pattern passed vacuously under React 19's silent
    // no-op-on-unmounted-setState.
    const unsubSpy = vi.fn();
    vi.spyOn(migrationStore, "onMigrationChange").mockImplementation((cb) => {
      void cb;
      return unsubSpy;
    });

    const { unmount } = renderHook(() => useMigrationStatus());
    expect(unsubSpy).not.toHaveBeenCalled();
    unmount();
    expect(unsubSpy).toHaveBeenCalledTimes(1);
  });
});
