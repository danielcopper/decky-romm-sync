import { describe, it, expect, beforeEach, vi } from "vitest";
import { showModal } from "@decky/ui";
import type { ReactElement } from "react";
import { showOfflineDriftModal } from "./OfflineDriftModal";

// The global @decky/ui stub (src/test-setup.ts) renders ConfirmModal as a
// pass-through <div> and exposes showModal as a vi.fn. We grab the element
// handed to showModal and read its props directly — mirroring SystemPage.test's
// lastConfirmModalProps helper — to invoke onOK / onCancel.
interface ConfirmModalProps {
  strTitle?: string;
  strDescription?: string;
  strOKButtonText?: string;
  strMiddleButtonText?: string;
  strCancelButtonText?: string;
  onOK?: () => void;
  onMiddleButton?: () => void;
  onCancel?: () => void;
}

function lastConfirmModalProps(): ConfirmModalProps {
  const calls = vi.mocked(showModal).mock.calls;
  const el = calls[calls.length - 1]?.[0] as ReactElement<ConfirmModalProps> | undefined;
  if (!el) throw new Error("showModal was not called");
  return el.props;
}

describe("OfflineDriftModal — showOfflineDriftModal", () => {
  beforeEach(() => {
    vi.mocked(showModal).mockClear();
  });

  it("renders the RomM Unreachable copy with Start Anyway / Retry / Cancel buttons", () => {
    void showOfflineDriftModal();
    const props = lastConfirmModalProps();
    expect(props.strTitle).toBe("RomM Unreachable");
    expect(props.strDescription).toContain("unsynced changes");
    expect(props.strOKButtonText).toBe("Start Anyway");
    expect(props.strMiddleButtonText).toBe("Retry connection");
    expect(props.strCancelButtonText).toBe("Cancel");
  });

  it("resolves 'start_anyway' when OK is pressed", async () => {
    const promise = showOfflineDriftModal();
    lastConfirmModalProps().onOK?.();
    await expect(promise).resolves.toBe("start_anyway");
  });

  it("resolves 'retry' when the middle button is pressed", async () => {
    const promise = showOfflineDriftModal();
    lastConfirmModalProps().onMiddleButton?.();
    await expect(promise).resolves.toBe("retry");
  });

  it("resolves 'cancel' when Cancel is pressed", async () => {
    const promise = showOfflineDriftModal();
    lastConfirmModalProps().onCancel?.();
    await expect(promise).resolves.toBe("cancel");
  });
});
