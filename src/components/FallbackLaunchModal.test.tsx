import { describe, it, expect, beforeEach, vi } from "vitest";
import { showModal } from "@decky/ui";
import type { ReactElement } from "react";
import { showFallbackLaunchModal } from "./FallbackLaunchModal";

// The global @decky/ui stub (src/test-setup.ts) renders ConfirmModal as a
// pass-through <div> and exposes showModal as a vi.fn. We grab the element
// handed to showModal and read its props directly — mirroring
// OfflineDriftModal.test's lastConfirmModalProps helper — to invoke onOK /
// onCancel. Every production call site mocks this helper, so this is the only
// place the real title/ternary/button wiring is exercised.
interface ConfirmModalProps {
  strTitle?: string;
  strDescription?: string;
  strOKButtonText?: string;
  strCancelButtonText?: string;
  onOK?: () => void;
  onCancel?: () => void;
}

function lastConfirmModalProps(): ConfirmModalProps {
  const calls = vi.mocked(showModal).mock.calls;
  const el = calls[calls.length - 1]?.[0] as ReactElement<ConfirmModalProps> | undefined;
  if (!el) throw new Error("showModal was not called");
  return el.props;
}

describe("FallbackLaunchModal — showFallbackLaunchModal", () => {
  beforeEach(() => {
    vi.mocked(showModal).mockClear();
  });

  it("renders the Save Sync Unavailable copy with Launch Anyway / Cancel buttons", () => {
    void showFallbackLaunchModal("some message");
    const props = lastConfirmModalProps();
    expect(props.strTitle).toBe("Save Sync Unavailable");
    expect(props.strOKButtonText).toBe("Launch Anyway");
    expect(props.strCancelButtonText).toBe("Cancel");
  });

  it("interpolates a backend message into the with-message description", () => {
    // The with-message ternary branch: `${message} — launch with local saves?`.
    void showFallbackLaunchModal("Device is not registered with RomM.");
    expect(lastConfirmModalProps().strDescription).toBe(
      "Device is not registered with RomM. — launch with local saves?",
    );
  });

  it("falls back to the generic description when no message is supplied", () => {
    // The no-message branch (undefined).
    void showFallbackLaunchModal();
    expect(lastConfirmModalProps().strDescription).toBe(
      "Couldn't sync saves with RomM server. Launch with local saves?",
    );
  });

  it("falls back to the generic description when the message is blank/whitespace", () => {
    // `message?.trim()` is falsy for "" and "   " → the generic branch.
    void showFallbackLaunchModal("   ");
    expect(lastConfirmModalProps().strDescription).toBe(
      "Couldn't sync saves with RomM server. Launch with local saves?",
    );
  });

  it("resolves true when Launch Anyway (OK) is pressed", async () => {
    const promise = showFallbackLaunchModal("msg");
    lastConfirmModalProps().onOK?.();
    await expect(promise).resolves.toBe(true);
  });

  it("resolves false when Cancel is pressed", async () => {
    const promise = showFallbackLaunchModal("msg");
    lastConfirmModalProps().onCancel?.();
    await expect(promise).resolves.toBe(false);
  });
});
