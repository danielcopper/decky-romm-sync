/**
 * Typed surface for the plugin's custom DOM events. Augments WindowEventMap
 * so addEventListener/dispatchEvent calls infer the right CustomEvent shape
 * without per-call `as CustomEvent<X>` casts.
 *
 * Add new event names + payloads here; consumers gain typed `detail` access
 * automatically. The discriminated union for `romm_data_changed` is the
 * source of truth — dispatch sites must match a variant, and switch-on-type
 * narrowing in handlers comes for free.
 */

import type { SaveStatus } from "./saves";

export type RommDataChangedDetail =
  | { type: "save_sync"; rom_id?: number; save_status?: SaveStatus; has_conflict?: boolean }
  | { type: "save_sync_settings"; save_sync_enabled: boolean }
  | { type: "metadata"; rom_id: number }
  | { type: "bios"; platform_slug: string }
  | { type: "core_changed"; platform_slug: string }
  | { type: "cover_refreshed"; rom_id: number };

export interface RommRomUninstalledDetail {
  rom_id: number;
}

export interface RommTabSwitchDetail {
  tab: string;
}

export interface RommConnectionChangedDetail {
  state: "checking" | "connected" | "offline";
}

declare global {
  interface WindowEventMap {
    romm_data_changed: CustomEvent<RommDataChangedDetail>;
    romm_rom_uninstalled: CustomEvent<RommRomUninstalledDetail>;
    romm_tab_switch: CustomEvent<RommTabSwitchDetail>;
    romm_connection_changed: CustomEvent<RommConnectionChangedDetail>;
  }
}
