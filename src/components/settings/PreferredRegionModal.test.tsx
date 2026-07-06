import { describe, it, expect, beforeEach, vi } from "vitest";
import { render, fireEvent } from "@testing-library/react";
import { cloneElement, createElement, type ReactElement } from "react";
import { showModal } from "@decky/ui";
import { showPreferredRegionModal } from "./PreferredRegionModal";
import { detach } from "../../utils/detach";

// Per-file @decky/ui mock: capture each ModalRoot's closeModal so the X-button /
// outside-click path is observable, and render DialogButtons as <button>.
type ModalCloseFn = (() => void) | undefined;
const capturedModalCloseFns: ModalCloseFn[] = [];

vi.mock("@decky/ui", () => {
  type AnyProps = Record<string, unknown> & { children?: unknown };
  return {
    ModalRoot: (p: AnyProps & { closeModal?: () => void }) => {
      capturedModalCloseFns.push(p.closeModal);
      return createElement("div", { "data-testid": "modal-root" }, p.children as never);
    },
    DialogButton: ({ children, onClick }: AnyProps) => createElement("button", { onClick }, children as never),
    showModal: vi.fn(),
  };
});

function buttonByText(container: HTMLElement, text: string): HTMLButtonElement {
  const btn = Array.from(container.querySelectorAll("button")).find((b) => b.textContent === text);
  if (!btn) throw new Error(`button "${text}" not found`);
  return btn as HTMLButtonElement;
}

interface ContentProps {
  oldLabel: string;
  newLabel: string;
  closeModal?: () => void;
  onDone: (proceed: boolean) => void;
}
function lastShownElement(): ReactElement<ContentProps> {
  const calls = vi.mocked(showModal).mock.calls;
  const el = calls[calls.length - 1]?.[0] as ReactElement<ContentProps> | undefined;
  if (!el) throw new Error("showModal was not called");
  return el;
}
function withCloseModal(el: ReactElement<ContentProps>, closeModal: () => void): ReactElement<ContentProps> {
  return cloneElement(el, { closeModal });
}

describe("PreferredRegionModal", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    capturedModalCloseFns.length = 0;
  });

  it("renders the title, label arrow, and the apply-at-next-sync explanation", () => {
    detach(showPreferredRegionModal("Default (World > USA > Europe)", "Japan"));
    const { container } = render(lastShownElement());
    expect(container.textContent).toContain("Change Preferred Region");
    expect(container.textContent).toContain("Default (World > USA > Europe) → Japan");
    expect(container.textContent).toContain("from now on");
    expect(container.textContent).toContain("already synced keep their current version and shortcut name");
  });

  it("resolves true when Save is clicked", async () => {
    const closeModal = vi.fn();
    const promise = showPreferredRegionModal("Default (World > USA > Europe)", "USA");
    const { container } = render(withCloseModal(lastShownElement(), closeModal));
    fireEvent.click(buttonByText(container, "Save"));
    expect(closeModal).toHaveBeenCalledTimes(1);
    await expect(promise).resolves.toBe(true);
  });

  it("resolves false when Cancel is clicked", async () => {
    const closeModal = vi.fn();
    const promise = showPreferredRegionModal("USA", "Japan");
    const { container } = render(withCloseModal(lastShownElement(), closeModal));
    fireEvent.click(buttonByText(container, "Cancel"));
    expect(closeModal).toHaveBeenCalledTimes(1);
    await expect(promise).resolves.toBe(false);
  });

  it("resolves false when ModalRoot closeModal fires (X / outside-click)", async () => {
    const promise = showPreferredRegionModal("USA", "Japan");
    render(lastShownElement());
    const modalClose = capturedModalCloseFns[capturedModalCloseFns.length - 1];
    expect(typeof modalClose).toBe("function");
    modalClose?.();
    await expect(promise).resolves.toBe(false);
  });

  it("Save does not throw when closeModal is undefined", async () => {
    const promise = showPreferredRegionModal("A", "B");
    const { container } = render(lastShownElement());
    expect(() => fireEvent.click(buttonByText(container, "Save"))).not.toThrow();
    await expect(promise).resolves.toBe(true);
  });
});
