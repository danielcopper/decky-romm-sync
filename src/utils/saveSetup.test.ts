import { describe, it, expect, vi } from "vitest";
import {
  applyLaunchGateSetupOutcome,
  applyWizardInitialSetupResult,
  applyWizardRetrySetupResult,
  resolveSaveSetupOutcome,
  SERVER_UNREACHABLE_WIZARD_MESSAGE,
  SERVER_UNREACHABLE_TOAST_BODY,
  type LaunchGateSetupDeps,
  type SaveSetupOutcome,
  type WizardRetryDeps,
  type WizardSetupDeps,
} from "./saveSetup";
import type { SaveSetupInfo } from "../types";

function makeInfo(overrides: Partial<SaveSetupInfo> = {}): SaveSetupInfo {
  return {
    has_local_saves: false,
    local_files: [],
    server_slots: [],
    default_slot: "default",
    slot_confirmed: false,
    active_slot: null,
    recommended_action: "auto_confirm_default",
    ...overrides,
  };
}

describe("resolveSaveSetupOutcome", () => {
  it("routes 'server_unreachable' to the unreachable outcome", () => {
    const info = makeInfo({ recommended_action: "server_unreachable", server_query_failed: true });
    expect(resolveSaveSetupOutcome(info)).toEqual({ kind: "server_unreachable" });
  });

  it("routes 'server_unreachable' to the unreachable outcome even when server_query_failed is absent", () => {
    // The enum alone is authoritative — server_query_failed is a redundant
    // mirror flag for call sites that branch on a boolean.
    const info = makeInfo({ recommended_action: "server_unreachable" });
    expect(resolveSaveSetupOutcome(info)).toEqual({ kind: "server_unreachable" });
  });

  it("routes 'auto_confirm_default' to the auto-confirm outcome with the default slot", () => {
    const info = makeInfo({ recommended_action: "auto_confirm_default", default_slot: "main" });
    expect(resolveSaveSetupOutcome(info)).toEqual({ kind: "auto_confirm", slot: "main" });
  });

  it("auto-confirms when the backend asks for the wizard but no server slots exist", () => {
    // Local saves but server empty — historic launch-gate behavior was to
    // auto-configure the default; preserved by resolveSaveSetupOutcome.
    const info = makeInfo({
      recommended_action: "show_wizard",
      has_local_saves: true,
      default_slot: "default",
    });
    expect(resolveSaveSetupOutcome(info)).toEqual({ kind: "auto_confirm", slot: "default" });
  });

  it("auto-confirms when both sides are empty under 'show_wizard'", () => {
    const info = makeInfo({ recommended_action: "show_wizard", default_slot: "default" });
    expect(resolveSaveSetupOutcome(info)).toEqual({ kind: "auto_confirm", slot: "default" });
  });

  it("requires user choice when the server reports any slots and the wizard is requested", () => {
    const info = makeInfo({
      recommended_action: "show_wizard",
      server_slots: [
        { slot: "default", saves: [], count: 1, latest_updated_at: "2026-01-01T00:00:00Z" },
      ],
    });
    expect(resolveSaveSetupOutcome(info)).toEqual({ kind: "needs_user_choice" });
  });
});

function makeLaunchGateDeps(overrides: Partial<LaunchGateSetupDeps> = {}): LaunchGateSetupDeps {
  return {
    rid: 42,
    confirmSlotChoice: vi.fn().mockResolvedValue({ success: true }),
    toast: vi.fn(),
    dispatchSavesTab: vi.fn(),
    ...overrides,
  };
}

describe("applyLaunchGateSetupOutcome", () => {
  it("toasts the unreachable copy, dispatches the saves-tab switch, and aborts on server_unreachable", async () => {
    const deps = makeLaunchGateDeps();
    const result = await applyLaunchGateSetupOutcome({ kind: "server_unreachable" }, deps);
    expect(result).toBe("abort");
    expect(deps.toast).toHaveBeenCalledWith(SERVER_UNREACHABLE_TOAST_BODY);
    expect(deps.dispatchSavesTab).toHaveBeenCalledOnce();
    expect(deps.confirmSlotChoice).not.toHaveBeenCalled();
  });

  it("calls confirmSlotChoice with the resolved slot and proceeds on auto_confirm", async () => {
    const deps = makeLaunchGateDeps();
    const outcome: SaveSetupOutcome = { kind: "auto_confirm", slot: "main" };
    const result = await applyLaunchGateSetupOutcome(outcome, deps);
    expect(result).toBe("proceed");
    expect(deps.confirmSlotChoice).toHaveBeenCalledWith(42, "main", null);
    expect(deps.toast).not.toHaveBeenCalled();
    expect(deps.dispatchSavesTab).not.toHaveBeenCalled();
  });

  it("toasts the configure-in-saves-tab copy, dispatches the tab switch, and aborts on needs_user_choice", async () => {
    const deps = makeLaunchGateDeps();
    const result = await applyLaunchGateSetupOutcome({ kind: "needs_user_choice" }, deps);
    expect(result).toBe("abort");
    expect(deps.toast).toHaveBeenCalledWith("Configure save sync in the Saves tab first");
    expect(deps.dispatchSavesTab).toHaveBeenCalledOnce();
    expect(deps.confirmSlotChoice).not.toHaveBeenCalled();
  });

  it("propagates a toast-callback exception on server_unreachable instead of swallowing it", async () => {
    // Regression guard for #619: if the launch-gate caller wraps this helper in
    // a try/catch that returns "proceed" on any error, swallowing the toast
    // exception would silently flip an abort decision into a launch. Surfacing
    // the throw forces the caller to keep its try shape narrow (network call
    // only) — see CustomPlayButton.ensureTrackingConfigured.
    const deps = makeLaunchGateDeps({
      toast: vi.fn().mockImplementation(() => {
        throw new Error("toast boom");
      }),
    });
    await expect(
      applyLaunchGateSetupOutcome({ kind: "server_unreachable" }, deps),
    ).rejects.toThrow("toast boom");
  });

  it("propagates a dispatchSavesTab exception on needs_user_choice instead of swallowing it", async () => {
    // Same guarantee on the user-choice branch — a broken event dispatch must
    // surface, not silently become a launch.
    const deps = makeLaunchGateDeps({
      dispatchSavesTab: vi.fn().mockImplementation(() => {
        throw new Error("dispatch boom");
      }),
    });
    await expect(
      applyLaunchGateSetupOutcome({ kind: "needs_user_choice" }, deps),
    ).rejects.toThrow("dispatch boom");
  });
});

function makeWizardDeps(overrides: Partial<WizardSetupDeps> = {}): WizardSetupDeps {
  return {
    romId: 7,
    confirmSlotChoice: vi.fn().mockResolvedValue({ success: true }),
    setError: vi.fn(),
    setConfirming: vi.fn(),
    setInfo: vi.fn(),
    logError: vi.fn(),
    onComplete: vi.fn(),
    isCancelled: () => false,
    ...overrides,
  };
}

describe("applyWizardInitialSetupResult", () => {
  it("sets the server-unreachable banner and bails on 'server_unreachable'", async () => {
    const deps = makeWizardDeps();
    const result = makeInfo({ recommended_action: "server_unreachable", server_query_failed: true });
    await applyWizardInitialSetupResult(result, deps);
    expect(deps.setError).toHaveBeenCalledWith(SERVER_UNREACHABLE_WIZARD_MESSAGE);
    expect(deps.setConfirming).not.toHaveBeenCalled();
    expect(deps.confirmSlotChoice).not.toHaveBeenCalled();
    expect(deps.setInfo).not.toHaveBeenCalled();
    expect(deps.onComplete).not.toHaveBeenCalled();
  });

  it("auto-confirms and calls onComplete on 'auto_confirm_default'", async () => {
    const deps = makeWizardDeps();
    const result = makeInfo({ recommended_action: "auto_confirm_default", default_slot: "alpha" });
    await applyWizardInitialSetupResult(result, deps);
    expect(deps.setConfirming).toHaveBeenCalledWith(true);
    expect(deps.confirmSlotChoice).toHaveBeenCalledWith(7, "alpha", null);
    expect(deps.onComplete).toHaveBeenCalledOnce();
    expect(deps.setError).not.toHaveBeenCalled();
    expect(deps.setInfo).not.toHaveBeenCalled();
  });

  it("skips onComplete when the caller is cancelled mid-confirm", async () => {
    let cancelled = false;
    const confirm = vi.fn().mockImplementation(() => {
      cancelled = true;
      return Promise.resolve({ success: true });
    });
    const deps = makeWizardDeps({ confirmSlotChoice: confirm, isCancelled: () => cancelled });
    const result = makeInfo({ recommended_action: "auto_confirm_default" });
    await applyWizardInitialSetupResult(result, deps);
    expect(deps.onComplete).not.toHaveBeenCalled();
    expect(deps.setError).not.toHaveBeenCalled();
  });

  it("recovers from a confirm failure with error banner, logger, info fallback, and resets confirming", async () => {
    const deps = makeWizardDeps({
      confirmSlotChoice: vi.fn().mockRejectedValue(new Error("boom")),
    });
    const result = makeInfo({ recommended_action: "auto_confirm_default", default_slot: "alpha" });
    await applyWizardInitialSetupResult(result, deps);
    expect(deps.setError).toHaveBeenCalledWith(expect.stringContaining("Auto-setup failed:"));
    expect(deps.logError).toHaveBeenCalledWith(expect.stringContaining("SlotSetupWizard auto-confirm failed:"));
    expect(deps.setConfirming).toHaveBeenNthCalledWith(1, true);
    expect(deps.setConfirming).toHaveBeenNthCalledWith(2, false);
    expect(deps.setInfo).toHaveBeenCalledWith(result);
    expect(deps.onComplete).not.toHaveBeenCalled();
  });

  it("skips error/info side effects when the caller is cancelled during a confirm failure", async () => {
    let cancelled = false;
    const confirm = vi.fn().mockImplementation(() => {
      cancelled = true;
      return Promise.reject(new Error("boom"));
    });
    const deps = makeWizardDeps({ confirmSlotChoice: confirm, isCancelled: () => cancelled });
    const result = makeInfo({ recommended_action: "auto_confirm_default" });
    await applyWizardInitialSetupResult(result, deps);
    expect(deps.setError).not.toHaveBeenCalled();
    expect(deps.logError).not.toHaveBeenCalled();
    expect(deps.setInfo).not.toHaveBeenCalled();
  });

  it("falls through to setInfo on 'show_wizard'", async () => {
    const deps = makeWizardDeps();
    const result = makeInfo({
      recommended_action: "show_wizard",
      server_slots: [
        { slot: "default", saves: [], count: 1, latest_updated_at: "2026-01-01T00:00:00Z" },
      ],
    });
    await applyWizardInitialSetupResult(result, deps);
    expect(deps.setInfo).toHaveBeenCalledWith(result);
    expect(deps.setError).not.toHaveBeenCalled();
    expect(deps.setConfirming).not.toHaveBeenCalled();
    expect(deps.confirmSlotChoice).not.toHaveBeenCalled();
  });
});

function makeRetryDeps(overrides: Partial<WizardRetryDeps> = {}): WizardRetryDeps {
  return {
    setError: vi.fn(),
    setLoading: vi.fn(),
    setInfo: vi.fn(),
    ...overrides,
  };
}

describe("applyWizardRetrySetupResult", () => {
  it("sets the unreachable banner and clears loading on 'server_unreachable'", () => {
    const deps = makeRetryDeps();
    const result = makeInfo({ recommended_action: "server_unreachable" });
    applyWizardRetrySetupResult(result, deps);
    expect(deps.setError).toHaveBeenCalledWith(SERVER_UNREACHABLE_WIZARD_MESSAGE);
    expect(deps.setLoading).toHaveBeenCalledWith(false);
    expect(deps.setInfo).not.toHaveBeenCalled();
  });

  it("sets the fetched info and clears loading on a non-unreachable result", () => {
    const deps = makeRetryDeps();
    const result = makeInfo({ recommended_action: "show_wizard" });
    applyWizardRetrySetupResult(result, deps);
    expect(deps.setInfo).toHaveBeenCalledWith(result);
    expect(deps.setLoading).toHaveBeenCalledWith(false);
    expect(deps.setError).not.toHaveBeenCalled();
  });

  it("does not auto-confirm even when the backend returns 'auto_confirm_default'", () => {
    // The retry button is user-initiated; the wizard should re-present the
    // (now-loaded) data rather than re-triggering the destructive auto-setup
    // path. The setInfo branch covers this case.
    const deps = makeRetryDeps();
    const result = makeInfo({ recommended_action: "auto_confirm_default" });
    applyWizardRetrySetupResult(result, deps);
    expect(deps.setInfo).toHaveBeenCalledWith(result);
    expect(deps.setLoading).toHaveBeenCalledWith(false);
    expect(deps.setError).not.toHaveBeenCalled();
  });
});
