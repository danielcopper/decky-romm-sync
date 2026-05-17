import { describe, it, expect } from "vitest";
import { resolveSaveSetupOutcome } from "./saveSetup";
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
