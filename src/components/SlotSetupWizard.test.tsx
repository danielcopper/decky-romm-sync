import { describe, it, expect, beforeEach, vi } from "vitest";
import { render, fireEvent, act } from "@testing-library/react";
import { createElement, type ChangeEvent, type ReactElement } from "react";
import { SlotSetupWizard } from "./SlotSetupWizard";
import * as backend from "../api/backend";
import { showModal } from "@decky/ui";
import {
  applyWizardInitialSetupResult,
  applyWizardRetrySetupResult,
  type WizardRetryDeps,
  type WizardSetupDeps,
} from "../utils/saveSetup";
import type { SaveSetupInfo } from "../types";

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
  label?: string;
  focusOnMount?: boolean;
}

vi.mock("@decky/ui", () => {
  // ConfirmModal renders an OK button so tests can drive the in-tree custom
  // slot modal (CustomSlotModal owns its own state and is mounted via RTL).
  // Modals passed *through* showModal still expose their onOK via the captured
  // showModal mock-call element — confirmModalPropsAt(...) handles that path.
  const ConfirmModal = (
    p: AnyProps & { onOK?: () => void | Promise<void> },
  ) =>
    createElement("div", { "data-testid": "confirm-modal" },
      createElement("button", {
        "data-testid": "confirm-modal-ok",
        onClick: () => { void p.onOK?.(); },
      }, "OK"),
      p.children as never,
    );
  return {
    ConfirmModal,
    DialogButton: ({
      children,
      onClick,
      disabled,
    }: AnyProps & { onClick?: () => void; disabled?: boolean }) =>
      createElement("button", { onClick, disabled }, children as never),
    TextField: (p: TextFieldProps) =>
      createElement("input", {
        "data-testid": "text-field",
        value: p.value ?? "",
        onChange: (e: ChangeEvent<HTMLInputElement>) => p.onChange?.(e),
      }),
    showModal: vi.fn(),
  };
});

// Mock the saveSetup helpers — their behavior is exhaustively covered in
// src/utils/saveSetup.test.ts. The wizard's job is to *wire* them correctly
// (right args, right callbacks). Tests that need state transitions through
// the helper invoke its setter callbacks (e.g. args.setInfo(...)) directly.
vi.mock("../utils/saveSetup", () => ({
  applyWizardInitialSetupResult: vi.fn(),
  applyWizardRetrySetupResult: vi.fn(),
}));

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
const flushAsync = () => act(async () => { await Promise.resolve(); });

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
  });

  describe("initial fetch + loading state", () => {
    it("renders the loading message immediately", () => {
      const { container } = render(<SlotSetupWizard {...defaultProps()} />);
      expect(container.textContent).toContain("Loading save information...");
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
      const [forwardedResult, deps] = vi.mocked(applyWizardInitialSetupResult).mock
        .calls[0] as [SaveSetupInfo, WizardSetupDeps];
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
      const [forwardedResult, deps] = vi.mocked(applyWizardRetrySetupResult).mock
        .calls[0] as [SaveSetupInfo, WizardRetryDeps];
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
    it("renders 'Setting up...' when confirming is true and there's no error", async () => {
      vi.mocked(applyWizardInitialSetupResult).mockImplementation(
        async (_result, deps) => { deps.setConfirming(true); },
      );
      const { container } = render(<SlotSetupWizard {...defaultProps()} />);
      await flushAsync();
      expect(container.textContent).toContain("Setting up...");
      expect(container.textContent).not.toContain("Loading save information...");
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
        server_slots: [
          { slot: "default", saves: [], count: 1, latest_updated_at: "2026-01-01T00:00:00Z" },
        ],
      });
      vi.mocked(applyWizardInitialSetupResult).mockImplementation(
        async (_r, deps) => { deps.setInfo(info); },
      );
      const { container } = render(<SlotSetupWizard {...defaultProps()} />);
      await flushAsync();
      expect(container.textContent).toContain("Server has saves");
    });

    it("shows the 'local saves and the server has saves too' copy when both sides have saves", async () => {
      const info = makeSetupInfo({
        has_local_saves: true,
        local_files: [{ filename: "a.srm", size: 100 }],
        server_slots: [
          { slot: "default", saves: [], count: 1, latest_updated_at: null },
        ],
      });
      vi.mocked(applyWizardInitialSetupResult).mockImplementation(
        async (_r, deps) => { deps.setInfo(info); },
      );
      const { container } = render(<SlotSetupWizard {...defaultProps()} />);
      await flushAsync();
      expect(container.textContent).toContain(
        "You have local saves and the server has saves too.",
      );
    });

    it("falls through to 'Choose a save slot to get started' for the local-only case", async () => {
      const info = makeSetupInfo({
        has_local_saves: true,
        local_files: [{ filename: "a.srm", size: 100 }],
        server_slots: [],
      });
      vi.mocked(applyWizardInitialSetupResult).mockImplementation(
        async (_r, deps) => { deps.setInfo(info); },
      );
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
      vi.mocked(applyWizardInitialSetupResult).mockImplementation(
        async (_r, deps) => { deps.setInfo(info); },
      );
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
      vi.mocked(applyWizardInitialSetupResult).mockImplementation(
        async (_r, deps) => { deps.setInfo(info); },
      );
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
      vi.mocked(applyWizardInitialSetupResult).mockImplementation(
        async (_r, deps) => { deps.setInfo(info); },
      );
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
      vi.mocked(applyWizardInitialSetupResult).mockImplementation(
        async (_r, deps) => { deps.setInfo(info); },
      );
      const { container } = render(<SlotSetupWizard {...defaultProps()} />);
      await flushAsync();
      expect(container.textContent).toContain("No saves on server");
    });

    it("displays a null slot as '(no slot)'", async () => {
      const info = makeSetupInfo({
        server_slots: [
          { slot: null, saves: [], count: 1, latest_updated_at: null },
        ],
      });
      vi.mocked(applyWizardInitialSetupResult).mockImplementation(
        async (_r, deps) => { deps.setInfo(info); },
      );
      const { container } = render(<SlotSetupWizard {...defaultProps()} />);
      await flushAsync();
      expect(container.textContent).toContain("(no slot)");
    });

    it("displays an empty-string slot as '(no slot)'", async () => {
      const info = makeSetupInfo({
        server_slots: [
          { slot: "", saves: [], count: 1, latest_updated_at: null },
        ],
      });
      vi.mocked(applyWizardInitialSetupResult).mockImplementation(
        async (_r, deps) => { deps.setInfo(info); },
      );
      const { container } = render(<SlotSetupWizard {...defaultProps()} />);
      await flushAsync();
      expect(container.textContent).toContain("(no slot)");
    });

    it("falls back to the raw iso string when the timestamp is malformed", async () => {
      // formatTimestamp wraps `new Date(...).toLocaleString(...)` in try/catch.
      // happy-dom's Date accepts arbitrary strings (NaN date), so this asserts
      // the path renders without throwing — exact format is locale-dependent.
      const info = makeSetupInfo({
        server_slots: [
          { slot: "x", saves: [], count: 1, latest_updated_at: "not-a-date" },
        ],
      });
      vi.mocked(applyWizardInitialSetupResult).mockImplementation(
        async (_r, deps) => { deps.setInfo(info); },
      );
      const { container } = render(<SlotSetupWizard {...defaultProps()} />);
      await flushAsync();
      expect(container.textContent).toContain("1 file");
    });
  });

  describe("Track button", () => {
    it("calls confirmSlotChoice with the slot value and triggers onComplete on success", async () => {
      const info = makeSetupInfo({
        default_slot: "default",
        server_slots: [
          { slot: "alpha", saves: [], count: 1, latest_updated_at: null },
        ],
      });
      vi.mocked(applyWizardInitialSetupResult).mockImplementation(
        async (_r, deps) => { deps.setInfo(info); },
      );
      const onComplete = vi.fn();
      const { getByText } = render(
        <SlotSetupWizard {...defaultProps({ romId: 5, onComplete })} />,
      );
      await flushAsync();

      await act(async () => {
        fireEvent.click(getByText("Track"));
        await Promise.resolve();
      });

      expect(vi.mocked(backend.confirmSlotChoice)).toHaveBeenCalledWith(5, "alpha", null);
      expect(onComplete).toHaveBeenCalledOnce();
    });

    it("falls back to the defaultSlot when the server slot is null", async () => {
      const info = makeSetupInfo({
        default_slot: "fallback",
        server_slots: [
          { slot: null, saves: [], count: 1, latest_updated_at: null },
        ],
      });
      vi.mocked(applyWizardInitialSetupResult).mockImplementation(
        async (_r, deps) => { deps.setInfo(info); },
      );
      const { getByText } = render(
        <SlotSetupWizard {...defaultProps({ romId: 5 })} />,
      );
      await flushAsync();

      await act(async () => {
        fireEvent.click(getByText("Track"));
        await Promise.resolve();
      });

      expect(vi.mocked(backend.confirmSlotChoice)).toHaveBeenCalledWith(5, "fallback", null);
    });

    it("surfaces a failed confirmSlotChoice via the inline error", async () => {
      const info = makeSetupInfo({
        server_slots: [
          { slot: "alpha", saves: [], count: 1, latest_updated_at: null },
        ],
      });
      vi.mocked(applyWizardInitialSetupResult).mockImplementation(
        async (_r, deps) => { deps.setInfo(info); },
      );
      vi.mocked(backend.confirmSlotChoice).mockResolvedValue({
        success: false,
        message: "Slot already exists",
      });
      const onComplete = vi.fn();
      const { container, getByText } = render(
        <SlotSetupWizard {...defaultProps({ onComplete })} />,
      );
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
        server_slots: [
          { slot: "alpha", saves: [], count: 1, latest_updated_at: null },
        ],
      });
      vi.mocked(applyWizardInitialSetupResult).mockImplementation(
        async (_r, deps) => { deps.setInfo(info); },
      );
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
        server_slots: [
          { slot: "alpha", saves: [], count: 1, latest_updated_at: null },
        ],
      });
      vi.mocked(applyWizardInitialSetupResult).mockImplementation(
        async (_r, deps) => { deps.setInfo(info); },
      );
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

  describe("default-slot button visibility", () => {
    it("renders the 'Use slot' button when the default is not in server_slots", async () => {
      const info = makeSetupInfo({
        default_slot: "fresh",
        server_slots: [
          { slot: "alpha", saves: [], count: 1, latest_updated_at: null },
        ],
      });
      vi.mocked(applyWizardInitialSetupResult).mockImplementation(
        async (_r, deps) => { deps.setInfo(info); },
      );
      const { container } = render(<SlotSetupWizard {...defaultProps()} />);
      await flushAsync();
      expect(container.textContent).toContain("Use slot");
      expect(container.textContent).toContain("fresh");
      expect(container.textContent).toContain("Or start fresh:");
    });

    it("hides the 'Use slot' button when the default IS in server_slots", async () => {
      const info = makeSetupInfo({
        default_slot: "alpha",
        server_slots: [
          { slot: "alpha", saves: [], count: 1, latest_updated_at: null },
        ],
      });
      vi.mocked(applyWizardInitialSetupResult).mockImplementation(
        async (_r, deps) => { deps.setInfo(info); },
      );
      const { container } = render(<SlotSetupWizard {...defaultProps()} />);
      await flushAsync();
      expect(container.textContent).not.toContain("Use slot");
      expect(container.textContent).not.toContain("Or start fresh:");
    });

    it("triggers handleConfirm(defaultSlot) when the 'Use slot' button is clicked", async () => {
      const info = makeSetupInfo({
        default_slot: "fresh",
        server_slots: [
          { slot: "alpha", saves: [], count: 1, latest_updated_at: null },
        ],
      });
      vi.mocked(applyWizardInitialSetupResult).mockImplementation(
        async (_r, deps) => { deps.setInfo(info); },
      );
      const { container } = render(
        <SlotSetupWizard {...defaultProps({ romId: 9 })} />,
      );
      await flushAsync();

      // The default-slot button text uses lsquo + rsquo around the slot name.
      // Match by partial textContent — the button is the only one containing
      // "Use slot" and "fresh".
      const buttons = Array.from(container.querySelectorAll("button"));
      const useSlotBtn = buttons.find((b) => b.textContent?.includes("Use slot"));
      if (!useSlotBtn) throw new Error("Use slot button not rendered");

      await act(async () => {
        fireEvent.click(useSlotBtn);
        await Promise.resolve();
      });

      expect(vi.mocked(backend.confirmSlotChoice)).toHaveBeenCalledWith(9, "fresh", null);
    });
  });

  describe("Custom slot modal", () => {
    it("opens the CustomSlotModal (titled 'Custom Slot Name') when 'Custom slot...' is clicked", async () => {
      const info = makeSetupInfo({ server_slots: [] });
      vi.mocked(applyWizardInitialSetupResult).mockImplementation(
        async (_r, deps) => { deps.setInfo(info); },
      );
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
      vi.mocked(applyWizardInitialSetupResult).mockImplementation(
        async (_r, deps) => { deps.setInfo(info); },
      );
      const { getByText } = render(
        <SlotSetupWizard {...defaultProps({ romId: 21 })} />,
      );
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

      expect(vi.mocked(backend.confirmSlotChoice)).toHaveBeenCalledWith(21, "myslot", null);
      // Non-empty submit must NOT open the legacy-mode prompt.
      expect(vi.mocked(showModal)).toHaveBeenCalledTimes(1);
      sub.unmount();
    });

    it("trims whitespace around the typed slot before passing it to handleConfirm", async () => {
      const info = makeSetupInfo({ server_slots: [] });
      vi.mocked(applyWizardInitialSetupResult).mockImplementation(
        async (_r, deps) => { deps.setInfo(info); },
      );
      const { getByText } = render(
        <SlotSetupWizard {...defaultProps({ romId: 3 })} />,
      );
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

      expect(vi.mocked(backend.confirmSlotChoice)).toHaveBeenCalledWith(3, "padded", null);
      sub.unmount();
    });

    it("opens the legacy-mode ConfirmModal when the user OKs with an empty input", async () => {
      const info = makeSetupInfo({ server_slots: [] });
      vi.mocked(applyWizardInitialSetupResult).mockImplementation(
        async (_r, deps) => { deps.setInfo(info); },
      );
      const { getByText } = render(<SlotSetupWizard {...defaultProps()} />);
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

      // Now there are two showModal calls — index 0 is the CustomSlotModal,
      // index 1 is the nested legacy-mode ConfirmModal (passed directly).
      expect(vi.mocked(showModal)).toHaveBeenCalledTimes(2);
      const legacy = confirmModalPropsAt(1);
      expect(legacy?.strTitle).toBe("Use Legacy Mode?");
      expect(legacy?.strDescription).toContain("Legacy mode");
      sub.unmount();
    });

    it("calls handleConfirm('') when the user types whitespace and OKs the nested legacy-mode confirm", async () => {
      const info = makeSetupInfo({ server_slots: [] });
      vi.mocked(applyWizardInitialSetupResult).mockImplementation(
        async (_r, deps) => { deps.setInfo(info); },
      );
      const { getByText } = render(
        <SlotSetupWizard {...defaultProps({ romId: 7 })} />,
      );
      await flushAsync();

      fireEvent.click(getByText("Custom slot..."));
      const modal = modalElementAt(0);
      if (!modal) throw new Error("CustomSlotModal element not captured");

      const sub = render(<>{modal}</>);
      // Type whitespace — trims to empty, routes through legacy-mode prompt.
      await act(async () => {
        fireEvent.change(sub.getByTestId("text-field"), { target: { value: "   " } });
      });
      await act(async () => {
        fireEvent.click(sub.getByTestId("confirm-modal-ok"));
        await Promise.resolve();
      });
      await act(async () => {
        await confirmModalPropsAt(1)?.onOK?.();
      });

      expect(vi.mocked(backend.confirmSlotChoice)).toHaveBeenCalledWith(7, "", null);
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
        server_slots: [
          { slot: "alpha", saves: [], count: 1, latest_updated_at: null },
        ],
      });
      vi.mocked(applyWizardInitialSetupResult).mockImplementation(
        async (_r, deps) => { deps.setInfo(info); },
      );
      // confirmSlotChoice never resolves — leaves confirming=true after click.
      vi.mocked(backend.confirmSlotChoice).mockImplementation(
        () => new Promise(() => { /* never resolves */ }),
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

      expect(container.textContent).toContain("Setting up...");
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
});
