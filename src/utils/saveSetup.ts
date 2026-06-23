/**
 * Pure decision logic and branch handlers for "what should the caller do with
 * a SaveSetupInfo response?". Two callers consume the same three-way outcome:
 * the wizard (`SlotSetupWizard`) and the launch gate
 * (`CustomPlayButton.ensureTrackingConfigured`). Anything that lives here can
 * be unit-tested without rendering the component or stubbing React.
 *
 * The "server_unreachable" branch exists because `recommended_action` carries
 * the explicit failure mode from the backend (see `get_save_setup_info`); the
 * call site MUST NOT treat an empty `server_slots` array as authoritative on
 * that path or it risks clobbering real server saves on first sync.
 */

import type { SaveSetupInfo } from "../types";

export type SaveSetupOutcome =
  | { kind: "server_unreachable" }
  | { kind: "auto_confirm"; slot: string }
  | { kind: "needs_user_choice" };

/** Resolve a SaveSetupInfo into the action its callers should take. */
export function resolveSaveSetupOutcome(info: SaveSetupInfo): SaveSetupOutcome {
  if (info.recommended_action === "server_unreachable") {
    return { kind: "server_unreachable" };
  }
  // Either the backend marked the response as "auto_confirm_default", or the
  // server is reachable but reports no saves on either side — both are safe
  // to auto-confirm with the default slot.
  if (info.recommended_action === "auto_confirm_default") {
    return { kind: "auto_confirm", slot: info.default_slot };
  }
  // Mirrors CustomPlayButton's pre-extraction branches: local-only or
  // empty-everywhere can still auto-confirm; only "server has saves" forces
  // the wizard.
  if (info.server_slots.length === 0) {
    return { kind: "auto_confirm", slot: info.default_slot };
  }
  return { kind: "needs_user_choice" };
}

/** User-facing copy for the server-unreachable branch, shared by the wizard
 *  banner and the launch-gate toast. */
export const SERVER_UNREACHABLE_WIZARD_MESSAGE =
  "RomM server is not reachable — cannot configure save slot. Retry once the server is back.";

export const SERVER_UNREACHABLE_TOAST_BODY =
  "Cannot configure save slot — RomM server is not reachable. Open the Saves tab to retry.";

const NEEDS_USER_CHOICE_TOAST_BODY = "Configure save sync in the Saves tab first";

/** Fallback toast body when `confirmSlotChoice` resolves to `success: false`
 *  without a message — used by both automated setup paths. */
const CONFIRM_FAILED_TOAST_BODY = "Couldn't configure save sync — open the Saves tab to finish setup.";

/** Side-effect bundle for the launch-gate handler. The component supplies
 *  Decky's `toaster.toast` and the global event dispatch; tests pass spies. */
export interface LaunchGateSetupDeps {
  /** ROM id passed to `confirmSlotChoice` on the auto-confirm branch. */
  rid: number;
  /** Resolves the user's chosen save slot on the backend. */
  confirmSlotChoice: (
    rid: number,
    slot: string | null,
    migrate: boolean,
    migrateFrom: string | null,
  ) => Promise<{ success?: boolean; message?: string } | undefined>;
  /** Shows a Decky toast — wrapped as a callback so the helper is dispatch-agnostic. */
  toast: (body: string) => void;
  /** Switches to the Saves tab — wrapped as a callback so the helper is
   *  dispatch-agnostic (component dispatches a `CustomEvent`; tests pass a spy). */
  dispatchSavesTab: () => void;
}

/** Dispatch a resolved `SaveSetupOutcome` from the launch gate (`Play` button)
 *  — returns "proceed" when the launch may continue or "abort" when the user
 *  was routed to the saves tab. */
export async function applyLaunchGateSetupOutcome(
  outcome: SaveSetupOutcome,
  deps: LaunchGateSetupDeps,
): Promise<"proceed" | "abort"> {
  if (outcome.kind === "server_unreachable") {
    deps.toast(SERVER_UNREACHABLE_TOAST_BODY);
    deps.dispatchSavesTab();
    return "abort";
  }
  if (outcome.kind === "auto_confirm") {
    // Auto-confirm of a named/default slot — never migrate.
    const result = await deps.confirmSlotChoice(deps.rid, outcome.slot, false, null);
    if (result?.success === false) {
      // A resolved failure (not a throw) must not let the launch proceed with
      // save tracking unconfigured — route to the Saves tab like the other
      // abort branches (#1009).
      deps.toast(result.message || CONFIRM_FAILED_TOAST_BODY);
      deps.dispatchSavesTab();
      return "abort";
    }
    return "proceed";
  }
  // Server has saves — user must configure in saves tab.
  deps.toast(NEEDS_USER_CHOICE_TOAST_BODY);
  deps.dispatchSavesTab();
  return "abort";
}

/** Side-effect bundle for the wizard's initial-fetch and retry handlers. The
 *  component supplies React's state setters and `confirmSlotChoice`; tests
 *  pass spies. */
export interface WizardSetupDeps {
  romId: number;
  confirmSlotChoice: (
    rid: number,
    slot: string | null,
    migrate: boolean,
    migrateFrom: string | null,
  ) => Promise<{ success?: boolean; message?: string } | undefined>;
  setError: (message: string | null) => void;
  setConfirming: (confirming: boolean) => void;
  setInfo: (info: SaveSetupInfo) => void;
  logError: (message: string) => void;
  onComplete: () => void;
  /** Returns true when the caller has been unmounted/superseded — guards the
   *  state setters after the awaited `confirmSlotChoice`. */
  isCancelled: () => boolean;
}

/** Apply the initial `getSaveSetupInfo` result inside `SlotSetupWizard`'s
 *  fetch effect. Routes server_unreachable to a held-wizard error banner,
 *  auto_confirm_default through a backend confirm, and everything else into
 *  the per-slot picker by calling `setInfo`. */
export async function applyWizardInitialSetupResult(result: SaveSetupInfo, deps: WizardSetupDeps): Promise<void> {
  if (result.recommended_action === "server_unreachable") {
    // Hold the wizard — auto-confirming default with an unknown server state
    // could overwrite real server saves the user already had on first
    // post-confirmation sync. Surface the error so the user can retry once
    // the server is reachable.
    deps.setError(SERVER_UNREACHABLE_WIZARD_MESSAGE);
    return;
  }
  if (result.recommended_action === "auto_confirm_default") {
    deps.setConfirming(true);
    try {
      // Auto-confirm of the default slot — never migrate.
      const confirmResult = await deps.confirmSlotChoice(deps.romId, result.default_slot, false, null);
      if (deps.isCancelled()) return;
      if (confirmResult?.success === false) {
        // Resolved failure (not a throw): mirror the catch branch — surface the
        // error and re-hydrate the wizard rather than completing with save
        // tracking unconfigured (#1009).
        deps.setError(`Auto-setup failed: ${confirmResult.message || "could not confirm slot"}`);
        deps.logError(`SlotSetupWizard auto-confirm returned success=false: ${confirmResult.message ?? ""}`);
        deps.setConfirming(false);
        deps.setInfo(result);
        return;
      }
      deps.onComplete();
    } catch (e) {
      if (!deps.isCancelled()) {
        deps.setError(`Auto-setup failed: ${e}`);
        deps.logError(`SlotSetupWizard auto-confirm failed: ${e}`);
        deps.setConfirming(false);
        deps.setInfo(result);
      }
    }
    return;
  }
  deps.setInfo(result);
}

/** Side-effect bundle for the wizard's retry button. Narrower than
 *  WizardSetupDeps because retry never auto-confirms. */
export interface WizardRetryDeps {
  setError: (message: string | null) => void;
  setLoading: (loading: boolean) => void;
  setInfo: (info: SaveSetupInfo) => void;
}

/** Apply the retry-button's `getSaveSetupInfo` result. Mirrors the
 *  server_unreachable handling of the initial fetch but skips the auto-confirm
 *  branch — retry is user-initiated and never auto-fires a destructive
 *  setup. */
export function applyWizardRetrySetupResult(result: SaveSetupInfo, deps: WizardRetryDeps): void {
  if (result.recommended_action === "server_unreachable") {
    deps.setError(SERVER_UNREACHABLE_WIZARD_MESSAGE);
    deps.setLoading(false);
    return;
  }
  deps.setInfo(result);
  deps.setLoading(false);
}
