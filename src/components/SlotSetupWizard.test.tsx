import { describe, it, expect, beforeEach, vi } from "vitest";
import { render, fireEvent, act } from "@testing-library/react";
import { createElement, type ChangeEvent, type KeyboardEvent, type ReactElement } from "react";
import { SlotSetupWizard } from "./SlotSetupWizard";
import * as backend from "../api/backend";
import { toaster } from "@decky/api";
import { showModal } from "@decky/ui";
import {
  applyWizardInitialSetupResult,
  applyWizardRetrySetupResult,
  type WizardRetryDeps,
  type WizardSetupDeps,
} from "../utils/saveSetup";
import type { SaveSetupInfo } from "../types";
import { detach } from "../utils/detach";
import {
  setRommConnectionState,
  getRommConnectionState,
  reportServerReachable,
  setServerRetryProgress,
} from "../utils/connectionState";

// Local @decky/ui re-mock — gives ConfirmModal an inline OK button so RTL can
// render-and-click the in-tree CustomSlotModal (which owns its own input state
// and is therefore mounted via a sub-render). Modals passed directly through
// showModal are still driven via their captured-element `.props.onOK`.
type AnyProps = Record<string, unknown> & { children?: unknown };
interface ConfirmModalProps {
  onOK?: () => void | Promise<void>;
  strTitle?: string;
  strDescription?: string;
  children?: unknown;
}
interface TextFieldProps {
  value?: string;
  onChange?: (e: ChangeEvent<HTMLInputElement>) => void;
  onKeyDown?: (e: KeyboardEvent<HTMLInputElement>) => void;
  label?: string;
  focusOnMount?: boolean;
}

vi.mock("@decky/ui", () => {
  // ConfirmModal renders an OK button so tests can drive the in-tree custom
  // slot modal (CustomSlotModal owns its own state and is mounted via RTL).
  // Modals passed *through* showModal still expose their onOK via the captured
  // showModal mock-call element — confirmModalPropsAt(...) handles that path.
  const ConfirmModal = (p: AnyProps & { onOK?: () => void | Promise<void> }) =>
    createElement(
      "div",
      { "data-testid": "confirm-modal" },
      createElement(
        "button",
        {
          "data-testid": "confirm-modal-ok",
          onClick: () => {
            detach(Promise.resolve(p.onOK?.()));
          },
        },
        "OK",
      ),
      p.children as never,
    );
  return {
    ConfirmModal,
    ModalRoot: (p: AnyProps) => createElement("div", { "data-testid": "modal-root" }, p.children as never),
    DialogButton: ({ children, onClick, disabled }: AnyProps & { onClick?: () => void; disabled?: boolean }) =>
      createElement("button", { onClick, disabled }, children as never),
    TextField: (p: TextFieldProps) =>
      createElement("input", {
        "data-testid": "text-field",
        value: p.value ?? "",
        onChange: (e: ChangeEvent<HTMLInputElement>) => p.onChange?.(e),
        onKeyDown: (e: KeyboardEvent<HTMLInputElement>) => p.onKeyDown?.(e),
      }),
    showModal: vi.fn(),
  };
});

// Mock the saveSetup helpers — their behavior is exhaustively covered in
// src/utils/saveSetup.test.ts. The wizard's job is to *wire* them correctly
// (right args, right callbacks). Tests that need state transitions through
// the helper invoke its setter callbacks (e.g. args.setInfo(...)) directly.
vi.mock("../utils/saveSetup", async (importActual) => {
  // Keep the real constants (SERVER_UNREACHABLE_WIZARD_MESSAGE etc.) the wizard
  // now imports; only the two apply-result helpers are stubbed.
  const actual = await importActual<typeof import("../utils/saveSetup")>();
  return {
    ...actual,
    applyWizardInitialSetupResult: vi.fn(),
    applyWizardRetrySetupResult: vi.fn(),
  };
});

function makeSetupInfo(overrides: Partial<SaveSetupInfo> = {}): SaveSetupInfo {
  return {
    has_local_saves: false,
    local_files: [],
    server_slots: [],
    default_slot: "default",
    slot_confirmed: false,
    active_slot: null,
    recommended_action: "show_wizard",
    ...overrides,
  };
}

function confirmModalPropsAt(idx: number): ConfirmModalProps | null {
  const calls = vi.mocked(showModal).mock.calls;
  const el = calls[idx]?.[0] as { props?: ConfirmModalProps } | undefined;
  return el?.props ?? null;
}

// Fetch the React element that was passed to the n-th showModal call. The
// custom-slot flow's first call passes a CustomSlotModal element (a local FC
// in SlotSetupWizard.tsx) — to drive its internal text-field state we have
// to render that element in its own RTL sub-tree.
function modalElementAt(idx: number): ReactElement | null {
  const calls = vi.mocked(showModal).mock.calls;
  const el = calls[idx]?.[0] as ReactElement | undefined;
  return el ?? null;
}

function defaultProps(
  overrides: Partial<React.ComponentProps<typeof SlotSetupWizard>> = {},
): React.ComponentProps<typeof SlotSetupWizard> {
  return {
    romId: 42,
    onComplete: vi.fn(),
    ...overrides,
  };
}

// Wait one microtask cycle so the initial fetchInfo() useEffect resolves.
const flushAsync = () =>
  act(async () => {
    await Promise.resolve();
  });

describe("SlotSetupWizard", () => {
  beforeEach(() => {
    // Reset implementations too (not just call history) — otherwise a previous
    // test's mockImplementation on applyWizardInitialSetupResult bleeds into
    // the next test and silently drives setInfo where we expected a no-op.
    vi.resetAllMocks();
    // Default: getSaveSetupInfo resolves to a benign show_wizard payload.
    // applyWizardInitialSetupResult is a no-op by default — tests that need
    // post-load state (info, error, confirming) override per-test by having
    // the mock invoke the relevant setter from its deps argument.
    vi.mocked(backend.getSaveSetupInfo).mockResolvedValue(makeSetupInfo());
    vi.mocked(backend.confirmSlotChoice).mockResolvedValue({
      success: true,
      message: "",
    });
    // The wizard now reads/feeds the shared connection store (#1345). Reset it
    // to a connected baseline so the known-offline fast path doesn't fire in the
    // tests that exercise the normal fetch, and clear any retry progress.
    setRommConnectionState("connected");
    setServerRetryProgress(null);
  });

  describe("initial fetch + loading state", () => {
    it("renders the connecting spinner immediately", () => {
      const { container } = render(<SlotSetupWizard {...defaultProps()} />);
      expect(container.textContent).toContain("Connecting to RomM…");
      expect(container.querySelector(".romm-throbber")).not.toBeNull();
    });

    it("calls getSaveSetupInfo with the romId on mount", async () => {
      render(<SlotSetupWizard {...defaultProps({ romId: 7 })} />);
      await flushAsync();
      expect(vi.mocked(backend.getSaveSetupInfo)).toHaveBeenCalledWith(7);
    });
  });

  describe("applyWizardInitialSetupResult wiring", () => {
    it("forwards the fetched info plus the full callback bag", async () => {
      const onComplete = vi.fn();
      const info = makeSetupInfo({ default_slot: "alpha" });
      vi.mocked(backend.getSaveSetupInfo).mockResolvedValue(info);
      render(<SlotSetupWizard {...defaultProps({ romId: 99, onComplete })} />);
      await flushAsync();
      expect(vi.mocked(applyWizardInitialSetupResult)).toHaveBeenCalledTimes(1);
      const [forwardedResult, deps] = vi.mocked(applyWizardInitialSetupResult).mock.calls[0] as [
        SaveSetupInfo,
        WizardSetupDeps,
      ];
      expect(forwardedResult).toBe(info);
      expect(deps.romId).toBe(99);
      expect(deps.confirmSlotChoice).toBe(backend.confirmSlotChoice);
      expect(deps.logError).toBe(backend.logError);
      expect(deps.onComplete).toBe(onComplete);
      expect(typeof deps.setError).toBe("function");
      expect(typeof deps.setConfirming).toBe("function");
      expect(typeof deps.setInfo).toBe("function");
      expect(typeof deps.isCancelled).toBe("function");
      // isCancelled is false on the mount path — only flips after unmount.
      expect(deps.isCancelled()).toBe(false);
    });
  });

  describe("fetch error path", () => {
    it("renders the error banner + Retry when getSaveSetupInfo rejects", async () => {
      vi.mocked(backend.getSaveSetupInfo).mockRejectedValue(new Error("boom"));
      const { container, getByText } = render(<SlotSetupWizard {...defaultProps()} />);
      await flushAsync();
      expect(container.textContent).toContain("Failed to load save setup info:");
      expect(container.textContent).toContain("boom");
      expect(getByText("Retry")).not.toBeNull();
    });
  });

  describe("retry button", () => {
    it("re-fetches and feeds applyWizardRetrySetupResult on click", async () => {
      vi.mocked(backend.getSaveSetupInfo).mockRejectedValueOnce(new Error("first fail"));
      const retryInfo = makeSetupInfo({ default_slot: "beta" });
      vi.mocked(backend.getSaveSetupInfo).mockResolvedValueOnce(retryInfo);
      const { getByText } = render(<SlotSetupWizard {...defaultProps({ romId: 11 })} />);
      await flushAsync();

      fireEvent.click(getByText("Retry"));
      await flushAsync();

      // getSaveSetupInfo called twice — once on mount, once for retry.
      expect(vi.mocked(backend.getSaveSetupInfo)).toHaveBeenCalledTimes(2);
      expect(vi.mocked(backend.getSaveSetupInfo)).toHaveBeenLastCalledWith(11);
      expect(vi.mocked(applyWizardRetrySetupResult)).toHaveBeenCalledTimes(1);
      const [forwardedResult, deps] = vi.mocked(applyWizardRetrySetupResult).mock.calls[0] as [
        SaveSetupInfo,
        WizardRetryDeps,
      ];
      expect(forwardedResult).toBe(retryInfo);
      expect(typeof deps.setError).toBe("function");
      expect(typeof deps.setLoading).toBe("function");
      expect(typeof deps.setInfo).toBe("function");
    });

    it("surfaces the retry-fetch rejection in the error banner", async () => {
      vi.mocked(backend.getSaveSetupInfo).mockRejectedValueOnce(new Error("first fail"));
      vi.mocked(backend.getSaveSetupInfo).mockRejectedValueOnce(new Error("retry boom"));
      const { container, getByText } = render(<SlotSetupWizard {...defaultProps()} />);
      await flushAsync();

      fireEvent.click(getByText("Retry"));
      await flushAsync();

      expect(container.textContent).toContain("Failed:");
      expect(container.textContent).toContain("retry boom");
      // applyWizardRetrySetupResult is never called when the fetch itself rejects.
      expect(vi.mocked(applyWizardRetrySetupResult)).not.toHaveBeenCalled();
    });
  });

  describe("confirming state", () => {
    it("renders 'Setting up…' when confirming is true and there's no error", async () => {
      vi.mocked(applyWizardInitialSetupResult).mockImplementation(async (_result, deps) => {
        deps.setConfirming(true);
      });
      const { container } = render(<SlotSetupWizard {...defaultProps()} />);
      await flushAsync();
      expect(container.textContent).toContain("Setting up…");
      expect(container.textContent).not.toContain("Connecting to RomM…");
    });
  });

  describe("null info renders null", () => {
    it("renders nothing when loading finishes without info and without error", async () => {
      // Default mock: applyWizardInitialSetupResult is a no-op, so info stays
      // null after loading completes. The component returns null in that path
      // (after the loading/confirming and error-without-data guards).
      const { container } = render(<SlotSetupWizard {...defaultProps()} />);
      await flushAsync();
      expect(container.textContent).toBe("");
    });
  });

  describe("normal render — getWizardDescription branches", () => {
    it("shows the 'Server has saves' copy when there are no local saves and the server has slots", async () => {
      const info = makeSetupInfo({
        has_local_saves: false,
        server_slots: [{ slot: "default", saves: [], count: 1, latest_updated_at: "2026-01-01T00:00:00Z" }],
      });
      vi.mocked(applyWizardInitialSetupResult).mockImplementation(async (_r, deps) => {
        deps.setInfo(info);
      });
      const { container } = render(<SlotSetupWizard {...defaultProps()} />);
      await flushAsync();
      expect(container.textContent).toContain("Server has saves");
    });

    it("shows the 'local saves and the server has saves too' copy when both sides have saves", async () => {
      const info = makeSetupInfo({
        has_local_saves: true,
        local_files: [{ filename: "a.srm", size: 100 }],
        server_slots: [{ slot: "default", saves: [], count: 1, latest_updated_at: null }],
      });
      vi.mocked(applyWizardInitialSetupResult).mockImplementation(async (_r, deps) => {
        deps.setInfo(info);
      });
      const { container } = render(<SlotSetupWizard {...defaultProps()} />);
      await flushAsync();
      expect(container.textContent).toContain("You have local saves and the server has saves too.");
    });

    it("falls through to 'Choose a save slot to get started' for the local-only case", async () => {
      const info = makeSetupInfo({
        has_local_saves: true,
        local_files: [{ filename: "a.srm", size: 100 }],
        server_slots: [],
      });
      vi.mocked(applyWizardInitialSetupResult).mockImplementation(async (_r, deps) => {
        deps.setInfo(info);
      });
      const { container } = render(<SlotSetupWizard {...defaultProps()} />);
      await flushAsync();
      expect(container.textContent).toContain("Choose a save slot to get started.");
    });
  });

  describe("local saves list", () => {
    it("renders each local file with its formatted size", async () => {
      const info = makeSetupInfo({
        local_files: [
          { filename: "tiny.srm", size: 512 },
          { filename: "medium.srm", size: 2048 },
          { filename: "big.srm", size: 5 * 1024 * 1024 },
        ],
      });
      vi.mocked(applyWizardInitialSetupResult).mockImplementation(async (_r, deps) => {
        deps.setInfo(info);
      });
      const { container } = render(<SlotSetupWizard {...defaultProps()} />);
      await flushAsync();
      expect(container.textContent).toContain("tiny.srm");
      expect(container.textContent).toContain("512 B");
      expect(container.textContent).toContain("medium.srm");
      expect(container.textContent).toContain("2.0 KB");
      expect(container.textContent).toContain("big.srm");
      expect(container.textContent).toContain("5.0 MB");
    });

    it("renders the 'No local saves found' empty state when there are no local files", async () => {
      const info = makeSetupInfo({ local_files: [] });
      vi.mocked(applyWizardInitialSetupResult).mockImplementation(async (_r, deps) => {
        deps.setInfo(info);
      });
      const { container } = render(<SlotSetupWizard {...defaultProps()} />);
      await flushAsync();
      expect(container.textContent).toContain("No local saves found");
    });
  });

  describe("server slots list", () => {
    it("renders each server slot with count + timestamp + Track button", async () => {
      const info = makeSetupInfo({
        server_slots: [
          {
            slot: "alpha",
            saves: [],
            count: 3,
            latest_updated_at: "2026-01-15T10:00:00Z",
          },
          { slot: "beta", saves: [], count: 1, latest_updated_at: null },
        ],
      });
      vi.mocked(applyWizardInitialSetupResult).mockImplementation(async (_r, deps) => {
        deps.setInfo(info);
      });
      const { container, getAllByText } = render(<SlotSetupWizard {...defaultProps()} />);
      await flushAsync();
      expect(container.textContent).toContain("alpha");
      expect(container.textContent).toContain("3 files");
      expect(container.textContent).toContain("beta");
      // Singular form for count == 1
      expect(container.textContent).toContain("1 file");
      expect(container.textContent).not.toContain("1 files");
      expect(getAllByText("Track").length).toBe(2);
    });

    it("renders the 'No saves on server' empty state when server_slots is empty", async () => {
      const info = makeSetupInfo({ server_slots: [] });
      vi.mocked(applyWizardInitialSetupResult).mockImplementation(async (_r, deps) => {
        deps.setInfo(info);
      });
      const { container } = render(<SlotSetupWizard {...defaultProps()} />);
      await flushAsync();
      expect(container.textContent).toContain("No saves on server");
    });

    it("displays a null slot as 'Legacy'", async () => {
      const info = makeSetupInfo({
        server_slots: [{ slot: null, saves: [], count: 1, latest_updated_at: null }],
      });
      vi.mocked(applyWizardInitialSetupResult).mockImplementation(async (_r, deps) => {
        deps.setInfo(info);
      });
      const { container } = render(<SlotSetupWizard {...defaultProps()} />);
      await flushAsync();
      expect(container.textContent).toContain("Legacy");
      expect(container.textContent).not.toContain("(no slot)");
    });

    it("displays an empty-string slot as 'Legacy'", async () => {
      const info = makeSetupInfo({
        server_slots: [{ slot: "", saves: [], count: 1, latest_updated_at: null }],
      });
      vi.mocked(applyWizardInitialSetupResult).mockImplementation(async (_r, deps) => {
        deps.setInfo(info);
      });
      const { container } = render(<SlotSetupWizard {...defaultProps()} />);
      await flushAsync();
      expect(container.textContent).toContain("Legacy");
      expect(container.textContent).not.toContain("(no slot)");
    });

    it("falls back to the raw iso string when the timestamp is malformed", async () => {
      // formatTimestamp wraps `new Date(...).toLocaleString(...)` in try/catch.
      // happy-dom's Date accepts arbitrary strings (NaN date), so this asserts
      // the path renders without throwing — exact format is locale-dependent.
      const info = makeSetupInfo({
        server_slots: [{ slot: "x", saves: [], count: 1, latest_updated_at: "not-a-date" }],
      });
      vi.mocked(applyWizardInitialSetupResult).mockImplementation(async (_r, deps) => {
        deps.setInfo(info);
      });
      const { container } = render(<SlotSetupWizard {...defaultProps()} />);
      await flushAsync();
      expect(container.textContent).toContain("1 file");
    });
  });

  describe("Track button", () => {
    it("calls confirmSlotChoice with the slot value and triggers onComplete on success", async () => {
      const info = makeSetupInfo({
        default_slot: "default",
        server_slots: [{ slot: "alpha", saves: [], count: 1, latest_updated_at: null }],
      });
      vi.mocked(applyWizardInitialSetupResult).mockImplementation(async (_r, deps) => {
        deps.setInfo(info);
      });
      const onComplete = vi.fn();
      const { getByText } = render(<SlotSetupWizard {...defaultProps({ romId: 5, onComplete })} />);
      await flushAsync();

      await act(async () => {
        fireEvent.click(getByText("Track"));
        await Promise.resolve();
      });

      expect(vi.mocked(backend.confirmSlotChoice)).toHaveBeenCalledWith(5, "alpha", false, null, false);
      expect(onComplete).toHaveBeenCalledOnce();
    });

    it("migrates the legacy (null) group into the default slot via a confirm modal", async () => {
      // #1276: legacy (no-slot) saves can no longer be tracked as-is. The null
      // server group's Track button offers to migrate them into the default
      // slot — confirmSlotChoice(rid, defaultSlot, migrate=true, from=null).
      const info = makeSetupInfo({
        default_slot: "fallback",
        server_slots: [{ slot: null, saves: [], count: 1, latest_updated_at: null }],
      });
      vi.mocked(applyWizardInitialSetupResult).mockImplementation(async (_r, deps) => {
        deps.setInfo(info);
      });
      const { getByText } = render(<SlotSetupWizard {...defaultProps({ romId: 5 })} />);
      await flushAsync();

      // Clicking Track on the legacy group opens the migrate-confirm modal.
      fireEvent.click(getByText("Track"));
      expect(vi.mocked(showModal)).toHaveBeenCalledTimes(1);
      const migrateModal = confirmModalPropsAt(0);
      expect(migrateModal?.strTitle).toBe("Migrate Legacy Saves?");
      expect(migrateModal?.strDescription).toContain("fallback");

      // OK migrates the legacy saves into the default slot.
      await act(async () => {
        await migrateModal?.onOK?.();
        await Promise.resolve();
      });

      expect(vi.mocked(backend.confirmSlotChoice)).toHaveBeenCalledWith(5, "fallback", true, null, false);
      // The legacy null slot is never confirmed as-is (retired, #1276).
      expect(vi.mocked(backend.confirmSlotChoice)).not.toHaveBeenCalledWith(5, null, false, null, false);
    });

    it("surfaces a failed confirmSlotChoice via the inline error", async () => {
      const info = makeSetupInfo({
        server_slots: [{ slot: "alpha", saves: [], count: 1, latest_updated_at: null }],
      });
      vi.mocked(applyWizardInitialSetupResult).mockImplementation(async (_r, deps) => {
        deps.setInfo(info);
      });
      vi.mocked(backend.confirmSlotChoice).mockResolvedValue({
        success: false,
        message: "Slot already exists",
      });
      const onComplete = vi.fn();
      const { container, getByText } = render(<SlotSetupWizard {...defaultProps({ onComplete })} />);
      await flushAsync();

      await act(async () => {
        fireEvent.click(getByText("Track"));
        await Promise.resolve();
      });

      expect(container.textContent).toContain("Slot already exists");
      expect(onComplete).not.toHaveBeenCalled();
    });

    it("falls back to a generic 'Slot confirmation failed' when the response carries no message", async () => {
      const info = makeSetupInfo({
        server_slots: [{ slot: "alpha", saves: [], count: 1, latest_updated_at: null }],
      });
      vi.mocked(applyWizardInitialSetupResult).mockImplementation(async (_r, deps) => {
        deps.setInfo(info);
      });
      vi.mocked(backend.confirmSlotChoice).mockResolvedValue({
        success: false,
        message: "",
      });
      const { container, getByText } = render(<SlotSetupWizard {...defaultProps()} />);
      await flushAsync();

      await act(async () => {
        fireEvent.click(getByText("Track"));
        await Promise.resolve();
      });

      expect(container.textContent).toContain("Slot confirmation failed");
    });

    it("surfaces a thrown confirmSlotChoice and logs via logError", async () => {
      const info = makeSetupInfo({
        server_slots: [{ slot: "alpha", saves: [], count: 1, latest_updated_at: null }],
      });
      vi.mocked(applyWizardInitialSetupResult).mockImplementation(async (_r, deps) => {
        deps.setInfo(info);
      });
      vi.mocked(backend.confirmSlotChoice).mockRejectedValue(new Error("network down"));
      const { container, getByText } = render(<SlotSetupWizard {...defaultProps()} />);
      await flushAsync();

      await act(async () => {
        fireEvent.click(getByText("Track"));
        await Promise.resolve();
      });

      expect(container.textContent).toContain("Failed to confirm slot:");
      expect(container.textContent).toContain("network down");
    });
  });

  describe("legacy migration content-based flow (#1498)", () => {
    const legacyInfo = () =>
      makeSetupInfo({
        default_slot: "default",
        server_slots: [{ slot: null, saves: [], count: 1, latest_updated_at: null }],
      });

    const conflictResult = () => ({
      success: false,
      needs_conflict_resolution: true,
      message: "A local save differs",
      conflicts: [
        {
          filename: "game.srm",
          server_save_id: 1,
          server_updated_at: "2026-01-01T00:00:00Z",
          server_size: 200,
          local_mtime: "2026-01-02T00:00:00Z",
          local_size: 100,
        },
      ],
    });

    it("shows the legacy migration explainer naming the target slot before Track is clicked", async () => {
      vi.mocked(applyWizardInitialSetupResult).mockImplementation(async (_r, deps) => {
        deps.setInfo(legacyInfo());
      });
      const { container } = render(<SlotSetupWizard {...defaultProps()} />);
      await flushAsync();
      expect(container.textContent).toContain("Tracking copies the legacy save into ‘default’");
      expect(container.textContent).not.toContain("into a named slot");
    });

    it("shows the start-fresh hint when there are local saves, without duplicating it for the custom route", async () => {
      const info = makeSetupInfo({
        default_slot: "fresh",
        has_local_saves: true,
        local_files: [{ filename: "game.srm", size: 100 }],
        server_slots: [{ slot: "alpha", saves: [], count: 1, latest_updated_at: null }],
      });
      vi.mocked(applyWizardInitialSetupResult).mockImplementation(async (_r, deps) => {
        deps.setInfo(info);
      });
      const { container } = render(<SlotSetupWizard {...defaultProps()} />);
      await flushAsync();
      expect(container.textContent).toContain("becomes the first save in ‘fresh’ on the next sync");
      // Both start-fresh routes are on screen — the hint must appear once, not twice.
      expect(container.textContent).not.toContain("in the new slot on the next sync");
    });

    it("shows the slot-agnostic next-sync hint for the custom route when the start-fresh block is hidden", async () => {
      // The default slot already exists on the server, so "Use slot ‘…’" (and its
      // named hint) is gone and "Custom slot..." is the only start-fresh route —
      // it still has to tell the user the local save uploads on the next sync.
      const info = makeSetupInfo({
        default_slot: "alpha",
        has_local_saves: true,
        local_files: [{ filename: "game.srm", size: 100 }],
        server_slots: [{ slot: "alpha", saves: [], count: 1, latest_updated_at: null }],
      });
      vi.mocked(applyWizardInitialSetupResult).mockImplementation(async (_r, deps) => {
        deps.setInfo(info);
      });
      const { container } = render(<SlotSetupWizard {...defaultProps()} />);
      await flushAsync();
      expect(container.textContent).not.toContain("Use slot");
      expect(container.textContent).toContain("Custom slot...");
      expect(container.textContent).toContain("becomes the first save in the new slot on the next sync");
      // Never names a slot the user isn't choosing.
      expect(container.textContent).not.toContain("in ‘alpha’ on the next sync");
    });

    it("shows no next-sync hint at all when there are no local saves", async () => {
      const info = makeSetupInfo({
        default_slot: "alpha",
        has_local_saves: false,
        local_files: [],
        server_slots: [{ slot: "alpha", saves: [], count: 1, latest_updated_at: null }],
      });
      vi.mocked(applyWizardInitialSetupResult).mockImplementation(async (_r, deps) => {
        deps.setInfo(info);
      });
      const { container } = render(<SlotSetupWizard {...defaultProps()} />);
      await flushAsync();
      expect(container.textContent).not.toContain("on the next sync");
    });

    it("fires the migration outcome toast naming the slot on success", async () => {
      vi.mocked(applyWizardInitialSetupResult).mockImplementation(async (_r, deps) => {
        deps.setInfo(legacyInfo());
      });
      vi.mocked(backend.confirmSlotChoice).mockResolvedValue({ success: true, message: "", migrated: 1, failed: 0 });
      const onComplete = vi.fn();
      const { getByText } = render(<SlotSetupWizard {...defaultProps({ romId: 5, onComplete })} />);
      await flushAsync();

      fireEvent.click(getByText("Track"));
      const migrateModal = confirmModalPropsAt(0);
      await act(async () => {
        await migrateModal?.onOK?.();
        await Promise.resolve();
      });

      expect(vi.mocked(backend.confirmSlotChoice)).toHaveBeenCalledWith(5, "default", true, null, false);
      expect(vi.mocked(toaster.toast)).toHaveBeenCalledWith({
        title: "RomM Sync",
        body: "Migrated 1 save into ‘default’. The legacy save stays in the read-only legacy bucket.",
      });
      expect(onComplete).toHaveBeenCalledOnce();
    });

    it("shows the message and stays open (retryable) on a wholesale migration failure", async () => {
      // A pre-apply failure (e.g. device not registered / server unreachable)
      // returns the canonical failure; the wizard must surface it and stay
      // usable — never confirm-and-close (#1498 review).
      vi.mocked(applyWizardInitialSetupResult).mockImplementation(async (_r, deps) => {
        deps.setInfo(legacyInfo());
      });
      vi.mocked(backend.confirmSlotChoice).mockResolvedValue({
        success: false,
        needs_conflict_resolution: false,
        reason: "device_not_registered",
        message: "This device isn't registered with RomM yet — retry in a moment.",
      });
      const onComplete = vi.fn();
      const { getByText, container } = render(<SlotSetupWizard {...defaultProps({ romId: 5, onComplete })} />);
      await flushAsync();

      fireEvent.click(getByText("Track"));
      const migrateModal = confirmModalPropsAt(0);
      await act(async () => {
        await migrateModal?.onOK?.();
        await Promise.resolve();
      });

      expect(container.textContent).toContain("This device isn't registered");
      expect(onComplete).not.toHaveBeenCalled();
      // Wizard stays usable — the Track button is still rendered for a retry.
      expect(getByText("Track")).toBeTruthy();
    });

    it("opens the conflict modal on needs_conflict_resolution and does not complete", async () => {
      vi.mocked(applyWizardInitialSetupResult).mockImplementation(async (_r, deps) => {
        deps.setInfo(legacyInfo());
      });
      vi.mocked(backend.confirmSlotChoice).mockResolvedValue(conflictResult());
      const onComplete = vi.fn();
      const { getByText } = render(<SlotSetupWizard {...defaultProps({ romId: 5, onComplete })} />);
      await flushAsync();

      fireEvent.click(getByText("Track"));
      const migrateModal = confirmModalPropsAt(0);
      await act(async () => {
        await migrateModal?.onOK?.();
        await Promise.resolve();
      });

      // The confirm modal (call 0) plus the conflict modal (call 1).
      expect(vi.mocked(showModal)).toHaveBeenCalledTimes(2);
      const conflictModal = modalElementAt(1);
      if (!conflictModal) throw new Error("conflict modal not captured");
      const sub = render(<>{conflictModal}</>);
      expect(sub.container.textContent).toContain("game.srm");
      expect(sub.container.textContent).toContain("Your local save");
      expect(sub.container.textContent).toContain("Legacy save on server");
      sub.unmount();
      // Nothing completed — the user still has to choose.
      expect(onComplete).not.toHaveBeenCalled();
    });

    it("'Replace local save' re-calls confirm with useServerOnConflict=true", async () => {
      vi.mocked(applyWizardInitialSetupResult).mockImplementation(async (_r, deps) => {
        deps.setInfo(legacyInfo());
      });
      vi.mocked(backend.confirmSlotChoice)
        .mockResolvedValueOnce(conflictResult())
        .mockResolvedValueOnce({ success: true, message: "", migrated: 1, failed: 0 });
      const { getByText } = render(<SlotSetupWizard {...defaultProps({ romId: 5 })} />);
      await flushAsync();

      fireEvent.click(getByText("Track"));
      const migrateModal = confirmModalPropsAt(0);
      await act(async () => {
        await migrateModal?.onOK?.();
        await Promise.resolve();
      });

      const sub = render(<>{modalElementAt(1)}</>);
      await act(async () => {
        fireEvent.click(sub.getByText("Replace local save"));
        await Promise.resolve();
      });
      expect(vi.mocked(backend.confirmSlotChoice)).toHaveBeenLastCalledWith(5, "default", true, null, true);
      sub.unmount();
    });

    it("Cancel changes nothing — no second call, wizard still open with Track available", async () => {
      // Cancelling the conflict dialog must leave the slot unconfirmed and the
      // wizard usable, so the user can take the start-fresh route instead.
      vi.mocked(applyWizardInitialSetupResult).mockImplementation(async (_r, deps) => {
        deps.setInfo(legacyInfo());
      });
      vi.mocked(backend.confirmSlotChoice).mockResolvedValue(conflictResult());
      const onComplete = vi.fn();
      const { getByText, container } = render(<SlotSetupWizard {...defaultProps({ romId: 5, onComplete })} />);
      await flushAsync();

      fireEvent.click(getByText("Track"));
      const migrateModal = confirmModalPropsAt(0);
      await act(async () => {
        await migrateModal?.onOK?.();
        await Promise.resolve();
      });

      // One probe call so far (the Track click).
      expect(vi.mocked(backend.confirmSlotChoice)).toHaveBeenCalledTimes(1);

      const sub = render(<>{modalElementAt(1)}</>);
      await act(async () => {
        fireEvent.click(sub.getByText("Cancel"));
        await Promise.resolve();
      });

      // No second call — nothing was migrated and nothing was confirmed.
      expect(vi.mocked(backend.confirmSlotChoice)).toHaveBeenCalledTimes(1);
      expect(onComplete).not.toHaveBeenCalled();
      // The wizard is still usable for the start-fresh route.
      expect(getByText("Track")).toBeTruthy();
      expect(container.textContent).toContain("Use slot");
      sub.unmount();
    });
  });

  describe("default-slot button visibility", () => {
    it("renders the 'Use slot' button when the default is not in server_slots", async () => {
      const info = makeSetupInfo({
        default_slot: "fresh",
        server_slots: [{ slot: "alpha", saves: [], count: 1, latest_updated_at: null }],
      });
      vi.mocked(applyWizardInitialSetupResult).mockImplementation(async (_r, deps) => {
        deps.setInfo(info);
      });
      const { container } = render(<SlotSetupWizard {...defaultProps()} />);
      await flushAsync();
      expect(container.textContent).toContain("Use slot");
      expect(container.textContent).toContain("fresh");
      expect(container.textContent).toContain("Or start fresh:");
    });

    it("hides the 'Use slot' button when the default IS in server_slots", async () => {
      const info = makeSetupInfo({
        default_slot: "alpha",
        server_slots: [{ slot: "alpha", saves: [], count: 1, latest_updated_at: null }],
      });
      vi.mocked(applyWizardInitialSetupResult).mockImplementation(async (_r, deps) => {
        deps.setInfo(info);
      });
      const { container } = render(<SlotSetupWizard {...defaultProps()} />);
      await flushAsync();
      expect(container.textContent).not.toContain("Use slot");
      expect(container.textContent).not.toContain("Or start fresh:");
    });

    it("triggers handleConfirm(defaultSlot) when the 'Use slot' button is clicked", async () => {
      const info = makeSetupInfo({
        default_slot: "fresh",
        server_slots: [{ slot: "alpha", saves: [], count: 1, latest_updated_at: null }],
      });
      vi.mocked(applyWizardInitialSetupResult).mockImplementation(async (_r, deps) => {
        deps.setInfo(info);
      });
      const { container } = render(<SlotSetupWizard {...defaultProps({ romId: 9 })} />);
      await flushAsync();

      // The default-slot button text uses lsquo + rsquo around the slot name.
      // Match by partial textContent — the button is the only one containing
      // "Use slot" and "fresh".
      const buttons = Array.from(container.querySelectorAll("button"));
      const useSlotBtn = buttons.find((b) => b.textContent.includes("Use slot"));
      if (!useSlotBtn) throw new Error("Use slot button not rendered");

      await act(async () => {
        fireEvent.click(useSlotBtn);
        await Promise.resolve();
      });

      expect(vi.mocked(backend.confirmSlotChoice)).toHaveBeenCalledWith(9, "fresh", false, null, false);
    });
  });

  describe("Custom slot modal", () => {
    it("opens the CustomSlotModal (titled 'Custom Slot Name') when 'Custom slot...' is clicked", async () => {
      const info = makeSetupInfo({ server_slots: [] });
      vi.mocked(applyWizardInitialSetupResult).mockImplementation(async (_r, deps) => {
        deps.setInfo(info);
      });
      const { getByText } = render(<SlotSetupWizard {...defaultProps()} />);
      await flushAsync();

      fireEvent.click(getByText("Custom slot..."));
      expect(vi.mocked(showModal)).toHaveBeenCalledTimes(1);

      // CustomSlotModal is a local FC — to assert the title we render the
      // captured element in its own RTL tree. The mocked ConfirmModal exposes
      // its strTitle via the rendered children sequence (textContent includes
      // the OK label only; we drive the modal via the captured element below).
      const modal = modalElementAt(0);
      if (!modal) throw new Error("CustomSlotModal element not captured");
      const sub = render(<>{modal}</>);
      // The TextField mock renders an input with the bound value.
      expect(sub.getByTestId("text-field")).not.toBeNull();
      sub.unmount();
    });

    it("submits the typed slot via handleConfirm when the user types a name and OKs", async () => {
      // Mutation guard: this test fails against the previous source where the
      // outer onClick's closure captured an empty customSlot. The CustomSlotModal
      // FC now owns its own input state, so the typed value reaches handleConfirm.
      const info = makeSetupInfo({ server_slots: [] });
      vi.mocked(applyWizardInitialSetupResult).mockImplementation(async (_r, deps) => {
        deps.setInfo(info);
      });
      const { getByText } = render(<SlotSetupWizard {...defaultProps({ romId: 21 })} />);
      await flushAsync();

      fireEvent.click(getByText("Custom slot..."));
      const modal = modalElementAt(0);
      if (!modal) throw new Error("CustomSlotModal element not captured");

      const sub = render(<>{modal}</>);
      await act(async () => {
        fireEvent.change(sub.getByTestId("text-field"), { target: { value: "myslot" } });
      });
      await act(async () => {
        fireEvent.click(sub.getByTestId("confirm-modal-ok"));
        await Promise.resolve();
      });

      expect(vi.mocked(backend.confirmSlotChoice)).toHaveBeenCalledWith(21, "myslot", false, null, false);
      // Non-empty submit must NOT open the legacy-mode prompt.
      expect(vi.mocked(showModal)).toHaveBeenCalledTimes(1);
      sub.unmount();
    });

    it("confirms the typed slot on Enter (on-screen keyboard)", async () => {
      const info = makeSetupInfo({ server_slots: [] });
      vi.mocked(applyWizardInitialSetupResult).mockImplementation(async (_r, deps) => {
        deps.setInfo(info);
      });
      const { getByText } = render(<SlotSetupWizard {...defaultProps({ romId: 21 })} />);
      await flushAsync();

      fireEvent.click(getByText("Custom slot..."));
      const modal = modalElementAt(0);
      if (!modal) throw new Error("CustomSlotModal element not captured");

      const sub = render(<>{modal}</>);
      await act(async () => {
        fireEvent.change(sub.getByTestId("text-field"), { target: { value: "  myslot  " } });
      });
      await act(async () => {
        fireEvent.keyDown(sub.getByTestId("text-field"), { key: "Enter" });
        await Promise.resolve();
      });

      // Enter trims and confirms, same as the OK button.
      expect(vi.mocked(backend.confirmSlotChoice)).toHaveBeenCalledWith(21, "myslot", false, null, false);
      sub.unmount();
    });

    it("ignores Enter on an empty custom slot (no confirm)", async () => {
      const info = makeSetupInfo({ server_slots: [] });
      vi.mocked(applyWizardInitialSetupResult).mockImplementation(async (_r, deps) => {
        deps.setInfo(info);
      });
      const { getByText } = render(<SlotSetupWizard {...defaultProps({ romId: 8 })} />);
      await flushAsync();

      fireEvent.click(getByText("Custom slot..."));
      const modal = modalElementAt(0);
      if (!modal) throw new Error("CustomSlotModal element not captured");

      const sub = render(<>{modal}</>);
      // Don't type anything — a blank Enter must be a no-op (unlike the OK button,
      // which sends "" straight to the backend guard).
      await act(async () => {
        fireEvent.keyDown(sub.getByTestId("text-field"), { key: "Enter" });
        await Promise.resolve();
      });

      expect(vi.mocked(backend.confirmSlotChoice)).not.toHaveBeenCalled();
      sub.unmount();
    });

    it("trims whitespace around the typed slot before passing it to handleConfirm", async () => {
      const info = makeSetupInfo({ server_slots: [] });
      vi.mocked(applyWizardInitialSetupResult).mockImplementation(async (_r, deps) => {
        deps.setInfo(info);
      });
      const { getByText } = render(<SlotSetupWizard {...defaultProps({ romId: 3 })} />);
      await flushAsync();

      fireEvent.click(getByText("Custom slot..."));
      const modal = modalElementAt(0);
      if (!modal) throw new Error("CustomSlotModal element not captured");

      const sub = render(<>{modal}</>);
      await act(async () => {
        fireEvent.change(sub.getByTestId("text-field"), { target: { value: "  padded  " } });
      });
      await act(async () => {
        fireEvent.click(sub.getByTestId("confirm-modal-ok"));
        await Promise.resolve();
      });

      expect(vi.mocked(backend.confirmSlotChoice)).toHaveBeenCalledWith(3, "padded", false, null, false);
      sub.unmount();
    });

    it("sends an empty custom name straight to the backend guard (no legacy-mode modal)", async () => {
      // #1276: an empty custom name is passed to the backend, which rejects it
      // via invalid_slot_name — it is NEVER reinterpreted as the retired legacy
      // no-slot mode, so no second modal opens.
      const info = makeSetupInfo({ server_slots: [] });
      vi.mocked(applyWizardInitialSetupResult).mockImplementation(async (_r, deps) => {
        deps.setInfo(info);
      });
      const { getByText } = render(<SlotSetupWizard {...defaultProps({ romId: 8 })} />);
      await flushAsync();

      fireEvent.click(getByText("Custom slot..."));
      const modal = modalElementAt(0);
      if (!modal) throw new Error("CustomSlotModal element not captured");

      const sub = render(<>{modal}</>);
      // Don't type anything — value stays "".
      await act(async () => {
        fireEvent.click(sub.getByTestId("confirm-modal-ok"));
        await Promise.resolve();
      });

      // The empty name goes straight to the backend guard; only the one modal opened.
      expect(vi.mocked(backend.confirmSlotChoice)).toHaveBeenCalledWith(8, "", false, null, false);
      expect(vi.mocked(showModal)).toHaveBeenCalledTimes(1);
      sub.unmount();
    });

    it("sends a whitespace-only custom name as '' to the backend guard (no legacy modal)", async () => {
      // #1276: whitespace trims to "" and is handed to the backend, which
      // rejects it — the retired legacy no-slot mode is never offered, and no
      // second modal opens.
      const info = makeSetupInfo({ server_slots: [] });
      vi.mocked(applyWizardInitialSetupResult).mockImplementation(async (_r, deps) => {
        deps.setInfo(info);
      });
      const { getByText } = render(<SlotSetupWizard {...defaultProps({ romId: 7 })} />);
      await flushAsync();

      fireEvent.click(getByText("Custom slot..."));
      const modal = modalElementAt(0);
      if (!modal) throw new Error("CustomSlotModal element not captured");

      const sub = render(<>{modal}</>);
      // Type whitespace — trims to empty, handed straight to the backend guard.
      await act(async () => {
        fireEvent.change(sub.getByTestId("text-field"), { target: { value: "   " } });
      });
      await act(async () => {
        fireEvent.click(sub.getByTestId("confirm-modal-ok"));
        await Promise.resolve();
      });

      expect(vi.mocked(backend.confirmSlotChoice)).toHaveBeenCalledWith(7, "", false, null, false);
      // Never confirms the retired legacy null slot, and no second modal opens.
      expect(vi.mocked(backend.confirmSlotChoice)).not.toHaveBeenCalledWith(7, null, false, null, false);
      expect(vi.mocked(showModal)).toHaveBeenCalledTimes(1);
      sub.unmount();
    });
  });

  describe("confirming transitions the wizard out of the normal layout", () => {
    it("replaces the action buttons with the 'Setting up...' view after Track is clicked", async () => {
      // handleConfirm always pairs setConfirming(true) with setError(null), so
      // the (loading || (confirming && !error)) guard at the top of render
      // wins — the wizard collapses to the loading-style view and the action
      // buttons aren't rendered at all.
      const info = makeSetupInfo({
        default_slot: "fresh",
        server_slots: [{ slot: "alpha", saves: [], count: 1, latest_updated_at: null }],
      });
      vi.mocked(applyWizardInitialSetupResult).mockImplementation(async (_r, deps) => {
        deps.setInfo(info);
      });
      // confirmSlotChoice never resolves — leaves confirming=true after click.
      vi.mocked(backend.confirmSlotChoice).mockImplementation(
        () =>
          new Promise(() => {
            /* never resolves */
          }),
      );
      const { container, getByText } = render(<SlotSetupWizard {...defaultProps()} />);
      await flushAsync();

      // Click Track — setConfirming(true) fires; the in-flight confirm pins
      // it. The confirming/!error guard means the normal layout is replaced
      // with the "Setting up..." view, so we assert that view instead of
      // poking individual button disabled flags (the buttons aren't rendered
      // in that branch).
      await act(async () => {
        fireEvent.click(getByText("Track"));
        await Promise.resolve();
      });

      expect(container.textContent).toContain("Setting up…");
      expect(container.textContent).not.toContain("Track");
    });
  });

  describe("cleanup on unmount", () => {
    it("reports isCancelled() = true once the component unmounts", async () => {
      let capturedDeps: WizardSetupDeps | null = null;
      vi.mocked(applyWizardInitialSetupResult).mockImplementation(async (_r, deps) => {
        capturedDeps = deps;
      });
      const { unmount } = render(<SlotSetupWizard {...defaultProps()} />);
      await flushAsync();
      expect(capturedDeps).not.toBeNull();
      // Before unmount: isCancelled returns false.
      const deps = capturedDeps as unknown as WizardSetupDeps;
      expect(deps.isCancelled()).toBe(false);
      unmount();
      // The useEffect cleanup flips the local `cancelled` flag — the captured
      // isCancelled closure now reports true. This proves the unmount-cleanup
      // pattern is wired correctly.
      expect(deps.isCancelled()).toBe(true);
    });
  });

  describe("offline connection integration (#1345)", () => {
    it("known-offline fast path: skips getSaveSetupInfo and shows the unreachable error + Retry", async () => {
      setRommConnectionState("offline");
      const { container, getByText } = render(<SlotSetupWizard {...defaultProps()} />);
      await flushAsync();
      // The full retry ladder is skipped — no backend call at all.
      expect(vi.mocked(backend.getSaveSetupInfo)).not.toHaveBeenCalled();
      expect(container.textContent).toContain("RomM server is not reachable");
      expect(getByText("Retry")).not.toBeNull();
    });

    it("reports the server reachable when the load returns a server-backed result", async () => {
      setRommConnectionState("checking");
      vi.mocked(backend.getSaveSetupInfo).mockResolvedValue(makeSetupInfo({ recommended_action: "show_wizard" }));
      render(<SlotSetupWizard {...defaultProps()} />);
      await flushAsync();
      expect(getRommConnectionState()).toBe("connected");
    });

    it("reports the server offline when the load returns recommended_action=server_unreachable", async () => {
      setRommConnectionState("checking");
      vi.mocked(backend.getSaveSetupInfo).mockResolvedValue(
        makeSetupInfo({ recommended_action: "server_unreachable", server_query_failed: true }),
      );
      render(<SlotSetupWizard {...defaultProps()} />);
      await flushAsync();
      expect(getRommConnectionState()).toBe("offline");
    });

    it("auto-reloads on reconnect after holding the offline error", async () => {
      setRommConnectionState("offline");
      render(<SlotSetupWizard {...defaultProps({ romId: 5 })} />);
      await flushAsync();
      // Fast path: nothing fetched yet.
      expect(vi.mocked(backend.getSaveSetupInfo)).not.toHaveBeenCalled();

      // The recovery probe reconnects — the wizard re-runs the load on the edge.
      vi.mocked(backend.getSaveSetupInfo).mockResolvedValue(makeSetupInfo());
      await act(async () => {
        reportServerReachable(true);
        await Promise.resolve();
      });
      await flushAsync();
      expect(vi.mocked(backend.getSaveSetupInfo)).toHaveBeenCalledWith(5);
    });

    it("shows the connecting spinner with the live attempt count while the load is in flight", async () => {
      setRommConnectionState("connected");
      // Never resolves — the wizard stays in the loading branch.
      vi.mocked(backend.getSaveSetupInfo).mockImplementation(() => new Promise(() => {}));
      const { container } = render(<SlotSetupWizard {...defaultProps()} />);
      expect(container.textContent).toContain("Connecting to RomM…");
      expect(container.querySelector(".romm-throbber")).not.toBeNull();
      act(() => setServerRetryProgress({ attempt: 2, maxAttempts: 3 }));
      expect(container.textContent).toContain("Connecting to RomM… (attempt 2/3)");
    });

    it("manual Retry feeds the store offline when it resolves server_unreachable", async () => {
      // Start held on an error so the Retry button is present, then have the
      // retry resolve unreachable and assert the store is driven offline.
      setRommConnectionState("checking");
      vi.mocked(backend.getSaveSetupInfo).mockRejectedValueOnce(new Error("first fail"));
      const { getByText } = render(<SlotSetupWizard {...defaultProps({ romId: 11 })} />);
      await flushAsync();

      vi.mocked(backend.getSaveSetupInfo).mockResolvedValueOnce(
        makeSetupInfo({ recommended_action: "server_unreachable", server_query_failed: true }),
      );
      await act(async () => {
        fireEvent.click(getByText("Retry"));
        await Promise.resolve();
      });
      await flushAsync();
      expect(getRommConnectionState()).toBe("offline");
    });

    it("clears stale retry progress when a fresh load starts (no leaked attempt suffix across loads)", async () => {
      // A retry during the first load left the shared store showing "(attempt
      // 2/3)" — exactly what the server_retry_progress event handler sets. A
      // NEW healthy load must reset it so ConnectingIndicator reads plain again.
      setRommConnectionState("connected");
      vi.mocked(backend.getSaveSetupInfo).mockResolvedValueOnce(makeSetupInfo());
      const { container, rerender } = render(<SlotSetupWizard {...defaultProps({ romId: 1 })} />);
      await flushAsync();

      act(() => setServerRetryProgress({ attempt: 2, maxAttempts: 3 }));

      // The next load stays in flight so ConnectingIndicator (the loading branch) renders.
      vi.mocked(backend.getSaveSetupInfo).mockImplementation(() => new Promise<never>(() => {}));
      rerender(<SlotSetupWizard {...defaultProps({ romId: 2 })} />);
      await flushAsync();

      // Fresh load cleared the store → plain label, no leaked "(attempt 2/3)".
      // (Drop the clear-on-start and this shows "attempt 2/3" and fails.)
      expect(container.textContent).toContain("Connecting to RomM…");
      expect(container.textContent).not.toContain("attempt 2/3");
    });

    it("manual Retry clears stale retry progress before re-fetching", async () => {
      setRommConnectionState("checking");
      vi.mocked(backend.getSaveSetupInfo).mockRejectedValueOnce(new Error("first fail"));
      const { container, getByText } = render(<SlotSetupWizard {...defaultProps()} />);
      await flushAsync();

      act(() => setServerRetryProgress({ attempt: 2, maxAttempts: 3 }));

      // Keep the retry fetch pending so the loading spinner renders.
      vi.mocked(backend.getSaveSetupInfo).mockImplementation(() => new Promise<never>(() => {}));
      await act(async () => {
        fireEvent.click(getByText("Retry"));
        await Promise.resolve();
      });

      expect(container.textContent).toContain("Connecting to RomM…");
      expect(container.textContent).not.toContain("attempt 2/3");
    });
  });
});
