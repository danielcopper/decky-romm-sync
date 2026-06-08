import { describe, it, expect, beforeEach, vi } from "vitest";
import { render, fireEvent } from "@testing-library/react";
import { cloneElement, createElement, type ReactElement } from "react";
import { showModal } from "@decky/ui";
import { showCoreChangeModal } from "./CoreChangeModal";
import { detach } from "../utils/detach";

// Per-file mock for @decky/ui. The global stub renders ModalRoot as a
// pass-through <div> but discards its `closeModal` prop. Here we capture
// every ModalRoot's closeModal so tests can invoke the X-button / outside-
// click handler that CoreChangeModalContent wires inline.
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
  const btn = Array.from(container.querySelectorAll("button")).find((b) => b.textContent === text);
  if (!btn) throw new Error(`button "${text}" not found`);
  return btn as HTMLButtonElement;
}

// Render the React element that showCoreChangeModal hands to showModal.
// CoreChangeModalContent is not exported; we capture the element handed to
// showModal and (optionally) re-clone it with a `closeModal` prop so the
// X-button / outside-click wiring is observable.
interface CoreChangeContentProps {
  oldLabel: string;
  newLabel: string;
  closeModal?: () => void;
  onDone: (proceed: boolean) => void;
}
function lastShownElement(): ReactElement<CoreChangeContentProps> {
  const calls = vi.mocked(showModal).mock.calls;
  const el = calls[calls.length - 1]?.[0] as ReactElement<CoreChangeContentProps> | undefined;
  if (!el) throw new Error("showModal was not called");
  return el;
}
function withCloseModal(
  el: ReactElement<CoreChangeContentProps>,
  closeModal: () => void,
): ReactElement<CoreChangeContentProps> {
  return cloneElement(el, { closeModal });
}

describe("CoreChangeModal", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    capturedModalCloseFns.length = 0;
  });

  describe("showCoreChangeModal — Promise wrapper", () => {
    it("resolves with true when Continue is clicked", async () => {
      const closeModal = vi.fn();
      const promise = showCoreChangeModal("PCSX-ReARMed", "Mednafen");
      // Re-render the captured element with a closeModal prop so the
      // showModal-supplied X-button path is observable too.
      const { container } = render(withCloseModal(lastShownElement(), closeModal));

      expect(container.textContent).toContain("Emulator Core Changed");
      expect(container.textContent).toContain("PCSX-ReARMed → Mednafen");

      fireEvent.click(buttonByText(container, "Continue"));
      expect(closeModal).toHaveBeenCalledTimes(1);
      await expect(promise).resolves.toBe(true);
    });

    it("resolves with false when Cancel is clicked", async () => {
      const closeModal = vi.fn();
      const promise = showCoreChangeModal("Old", "New");
      const { container } = render(withCloseModal(lastShownElement(), closeModal));

      fireEvent.click(buttonByText(container, "Cancel"));
      expect(closeModal).toHaveBeenCalledTimes(1);
      await expect(promise).resolves.toBe(false);
    });

    it("resolves with false when ModalRoot's closeModal fires (X / outside-click)", async () => {
      const closeModal = vi.fn();
      const promise = showCoreChangeModal("Old", "New");
      render(withCloseModal(lastShownElement(), closeModal));

      // ModalRoot's closeModal is `() => { closeModal?.(); onDone(false); }`.
      const modalClose = capturedModalCloseFns[capturedModalCloseFns.length - 1];
      expect(typeof modalClose).toBe("function");
      modalClose?.();
      expect(closeModal).toHaveBeenCalledTimes(1);
      await expect(promise).resolves.toBe(false);
    });
  });

  describe("CoreChangeModalContent — rendering", () => {
    it("always renders title, label arrow, and the Save Compatibility Warning", () => {
      // Drive via showCoreChangeModal so we don't depend on the non-exported FC.
      detach(showCoreChangeModal("CoreA", "CoreB"));
      const { container } = render(lastShownElement());

      expect(container.textContent).toContain("Emulator Core Changed");
      expect(container.textContent).toContain("CoreA → CoreB");
      expect(container.textContent).toContain("Save Compatibility Warning");
    });
  });

  describe("CoreChangeModalContent — closeModal optional", () => {
    it("Continue does not throw when closeModal is undefined", async () => {
      const promise = showCoreChangeModal("X", "Y");
      // Render the element as supplied (closeModal is undefined — showModal
      // would normally inject one, but the contract allows it to be missing).
      const { container } = render(lastShownElement());

      expect(() => fireEvent.click(buttonByText(container, "Continue"))).not.toThrow();
      await expect(promise).resolves.toBe(true);
    });

    it("Cancel does not throw when closeModal is undefined", async () => {
      const promise = showCoreChangeModal("X", "Y");
      const { container } = render(lastShownElement());

      expect(() => fireEvent.click(buttonByText(container, "Cancel"))).not.toThrow();
      await expect(promise).resolves.toBe(false);
    });

    it("ModalRoot's closeModal does not throw when closeModal is undefined", async () => {
      const promise = showCoreChangeModal("X", "Y");
      render(lastShownElement());

      const modalClose = capturedModalCloseFns[capturedModalCloseFns.length - 1];
      expect(() => modalClose?.()).not.toThrow();
      await expect(promise).resolves.toBe(false);
    });
  });
});
