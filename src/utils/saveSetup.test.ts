import { describe, it, expect, vi } from "vitest";
import {
  applyLaunchGateSetupOutcome,
  applyWizardInitialSetupResult,
  applyWizardRetrySetupResult,
  legacyConflictReplaceNotice,
  legacyMigrateConfirmDescription,
  legacyTrackExplainer,
  resolveSaveSetupOutcome,
  startFreshHint,
  startFreshHintNewSlot,
  wizardMigrationOutcomeToastBody,
  SERVER_UNREACHABLE_WIZARD_MESSAGE,
  SERVER_UNREACHABLE_TOAST_BODY,
  NOT_FOUND_WIZARD_MESSAGE,
  NOT_FOUND_TOAST_BODY,
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

  it("routes 'not_found' to its own outcome, NOT to auto-confirm", () => {
    // The empty server_slots is no more authoritative on a 404 than on an
    // outage, so this must not fall through to the auto-confirm branch (#1570).
    const info = makeInfo({ recommended_action: "not_found", server_query_failed: true });
    expect(resolveSaveSetupOutcome(info)).toEqual({ kind: "not_found" });
  });

  it("routes 'not_found' even with local saves and an empty server list present", () => {
    // The exact shape that would otherwise auto-confirm under 'show_wizard'.
    const info = makeInfo({
      recommended_action: "not_found",
      has_local_saves: true,
      server_slots: [],
      default_slot: "default",
    });
    expect(resolveSaveSetupOutcome(info)).not.toEqual({ kind: "auto_confirm", slot: "default" });
    expect(resolveSaveSetupOutcome(info)).toEqual({ kind: "not_found" });
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
      server_slots: [{ slot: "default", saves: [], count: 1, latest_updated_at: "2026-01-01T00:00:00Z" }],
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

  it("aborts the launch on not_found too, with its own copy", async () => {
    // The launch must NOT proceed with save tracking unconfigured just
    // because the failure was a 404 rather than an outage (#1570).
    const deps = makeLaunchGateDeps();
    const result = await applyLaunchGateSetupOutcome({ kind: "not_found" }, deps);
    expect(result).toBe("abort");
    expect(deps.toast).toHaveBeenCalledWith(NOT_FOUND_TOAST_BODY);
    expect(deps.dispatchSavesTab).toHaveBeenCalledOnce();
    expect(deps.confirmSlotChoice).not.toHaveBeenCalled();
  });

  it("calls confirmSlotChoice with the resolved slot and proceeds on auto_confirm", async () => {
    const deps = makeLaunchGateDeps();
    const outcome: SaveSetupOutcome = { kind: "auto_confirm", slot: "main" };
    const result = await applyLaunchGateSetupOutcome(outcome, deps);
    expect(result).toBe("proceed");
    expect(deps.confirmSlotChoice).toHaveBeenCalledWith(42, "main", false, null, false);
    expect(deps.toast).not.toHaveBeenCalled();
    expect(deps.dispatchSavesTab).not.toHaveBeenCalled();
  });

  it("aborts and routes to the saves tab when confirmSlotChoice resolves success:false", async () => {
    // #1009: a resolved failure (not a throw) must not let the launch proceed
    // with save tracking unconfigured. The backend returns {success:false}
    // without throwing, so only this branch catches it.
    const deps = makeLaunchGateDeps({
      confirmSlotChoice: vi.fn().mockResolvedValue({ success: false, message: "slot taken" }),
    });
    const result = await applyLaunchGateSetupOutcome({ kind: "auto_confirm", slot: "main" }, deps);
    expect(result).toBe("abort");
    expect(deps.confirmSlotChoice).toHaveBeenCalledWith(42, "main", false, null, false);
    expect(deps.toast).toHaveBeenCalledWith("slot taken");
    expect(deps.dispatchSavesTab).toHaveBeenCalledOnce();
  });

  it("falls back to the generic toast when confirmSlotChoice fails without a message", async () => {
    const deps = makeLaunchGateDeps({
      confirmSlotChoice: vi.fn().mockResolvedValue({ success: false }),
    });
    const result = await applyLaunchGateSetupOutcome({ kind: "auto_confirm", slot: "main" }, deps);
    expect(result).toBe("abort");
    expect(deps.toast).toHaveBeenCalledWith(expect.stringContaining("Couldn't configure save sync"));
    expect(deps.dispatchSavesTab).toHaveBeenCalledOnce();
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
    await expect(applyLaunchGateSetupOutcome({ kind: "server_unreachable" }, deps)).rejects.toThrow("toast boom");
  });

  it("propagates a dispatchSavesTab exception on needs_user_choice instead of swallowing it", async () => {
    // Same guarantee on the user-choice branch — a broken event dispatch must
    // surface, not silently become a launch.
    const deps = makeLaunchGateDeps({
      dispatchSavesTab: vi.fn().mockImplementation(() => {
        throw new Error("dispatch boom");
      }),
    });
    await expect(applyLaunchGateSetupOutcome({ kind: "needs_user_choice" }, deps)).rejects.toThrow("dispatch boom");
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

  it("sets the not-found banner and bails, never auto-confirming", async () => {
    // Same hold as server_unreachable — the wizard must not configure a slot
    // against an unproven server view just because the cause was a 404 (#1570).
    const deps = makeWizardDeps();
    const result = makeInfo({ recommended_action: "not_found", server_query_failed: true });
    await applyWizardInitialSetupResult(result, deps);
    expect(deps.setError).toHaveBeenCalledWith(NOT_FOUND_WIZARD_MESSAGE);
    expect(deps.setConfirming).not.toHaveBeenCalled();
    expect(deps.confirmSlotChoice).not.toHaveBeenCalled();
    expect(deps.setInfo).not.toHaveBeenCalled();
    expect(deps.onComplete).not.toHaveBeenCalled();
  });

  it("the not-found copy names what RomM could not find, not that saves are absent", () => {
    // The 404 can come from the DEVICE registration, so claiming the game has
    // no saves would swap one lie for a more specific one (#1570).
    expect(NOT_FOUND_WIZARD_MESSAGE).not.toMatch(/no saves|has no save|without saves/i);
    expect(NOT_FOUND_WIZARD_MESSAGE).not.toMatch(/unreachable|not reachable|offline/i);
    expect(NOT_FOUND_TOAST_BODY).not.toMatch(/unreachable|not reachable|offline/i);
  });

  it("auto-confirms and calls onComplete on 'auto_confirm_default'", async () => {
    const deps = makeWizardDeps();
    const result = makeInfo({ recommended_action: "auto_confirm_default", default_slot: "alpha" });
    await applyWizardInitialSetupResult(result, deps);
    expect(deps.setConfirming).toHaveBeenCalledWith(true);
    expect(deps.confirmSlotChoice).toHaveBeenCalledWith(7, "alpha", false, null, false);
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

  it("recovers from a resolved success:false confirm without calling onComplete", async () => {
    // #1009: a {success:false} that does NOT throw is exactly what the backend
    // returns on a failed confirm — the automated path must treat it like the
    // manual path (SlotSetupWizard), not complete with tracking unconfigured.
    const deps = makeWizardDeps({
      confirmSlotChoice: vi.fn().mockResolvedValue({ success: false, message: "slot taken" }),
    });
    const result = makeInfo({ recommended_action: "auto_confirm_default", default_slot: "alpha" });
    await applyWizardInitialSetupResult(result, deps);
    expect(deps.setError).toHaveBeenCalledWith(expect.stringContaining("slot taken"));
    expect(deps.logError).toHaveBeenCalledWith(expect.stringContaining("success=false"));
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
      server_slots: [{ slot: "default", saves: [], count: 1, latest_updated_at: "2026-01-01T00:00:00Z" }],
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

  it("sets the not-found banner and clears loading on 'not_found'", () => {
    const deps = makeRetryDeps();
    const result = makeInfo({ recommended_action: "not_found" });
    applyWizardRetrySetupResult(result, deps);
    expect(deps.setError).toHaveBeenCalledWith(NOT_FOUND_WIZARD_MESSAGE);
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

describe("legacy-migration copy (#1498)", () => {
  it("the Track explainer names the concrete target slot and says the legacy save is left untouched", () => {
    const body = legacyTrackExplainer("default");
    expect(body).toContain("copies the legacy save");
    // Names the resolved slot — never "a named slot", which reads as still-open.
    expect(body).toContain("‘default’");
    expect(body).not.toContain("a named slot");
    expect(body).toContain("left untouched");
  });

  it("the conflict notice states the backup-and-replace and that cancelling changes nothing", () => {
    const body = legacyConflictReplaceNotice("default");
    expect(body).toContain(".romm-backup");
    expect(body).toContain("replaced with the legacy save");
    expect(body).toContain("nothing changes");
    expect(body).toContain("‘default’");
  });

  it("the migrate confirm description names the slot and promises a confirmation, not a keep-choice", () => {
    const body = legacyMigrateConfirmDescription("default");
    expect(body).toContain("‘default’");
    expect(body).toContain("differs");
    expect(body).toContain("confirm before anything is replaced");
    // The dialog is confirm-or-cancel — never promise a choose-which-to-keep.
    expect(body).not.toContain("which to keep");
  });

  it("the start-fresh hint names the slot and points at the next sync", () => {
    const body = startFreshHint("main");
    expect(body).toContain("‘main’");
    expect(body).toContain("next sync");
  });

  it("the custom-slot variant makes the same promise without naming a slot", () => {
    const body = startFreshHintNewSlot();
    expect(body).toContain("the new slot");
    expect(body).toContain("next sync");
    // The custom name isn't known until submit — never name a concrete slot here.
    expect(body).not.toContain("‘");
  });

  describe("wizardMigrationOutcomeToastBody", () => {
    it("names the slot and count and reassures the legacy save stays (singular)", () => {
      expect(wizardMigrationOutcomeToastBody(1, 0, "default")).toBe(
        "Migrated 1 save into ‘default’. The legacy save stays in the read-only legacy bucket.",
      );
    });

    it("pluralizes the count and the legacy-stays clause when more than one save migrated", () => {
      expect(wizardMigrationOutcomeToastBody(2, 0, "default")).toBe(
        "Migrated 2 saves into ‘default’. The legacy saves stay in the read-only legacy bucket.",
      );
    });

    it("reports failures alongside successes and still reassures", () => {
      expect(wizardMigrationOutcomeToastBody(1, 1, "slotA")).toBe(
        "Migrated 1 save into ‘slotA’; 1 could not be migrated. The legacy save stays in the read-only legacy bucket.",
      );
    });

    it("names the could-not-migrate count when nothing succeeded (no legacy-stays clause needed)", () => {
      expect(wizardMigrationOutcomeToastBody(0, 2, "slotA")).toBe("Could not migrate 2 saves into ‘slotA’");
    });

    it("returns null when nothing was attempted", () => {
      expect(wizardMigrationOutcomeToastBody(0, 0, "default")).toBeNull();
    });
  });
});
