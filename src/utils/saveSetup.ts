/**
 * Pure decision logic for "what should the caller do with a SaveSetupInfo
 * response?". Mirrors the three-way outcome both the wizard
 * (SlotSetupWizard) and the launch gate (CustomPlayButton.ensureTrackingConfigured)
 * need so the branch is testable in isolation from React/Decky APIs.
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
