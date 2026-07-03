/**
 * Builders for `EmulatorOption` test fixtures (#1210).
 *
 * The picker payloads (`get_platform_core_info` / `get_firmware_status`) carry
 * a classified emulator list; these helpers keep the full six-field shape in one
 * place so tests read as `libretroEmu("mgba_libretro", "mGBA", true)` instead of
 * repeating `{ label, kind, core_so, is_default, bakeable, reason }`.
 */

import type { EmulatorOption } from "../types";

/** A bakeable libretro emulator option (the common case). */
export function libretroEmu(core_so: string, label: string, is_default = false): EmulatorOption {
  return { label, kind: "libretro", core_so, is_default, bakeable: true, reason: null };
}

/** A standalone emulator option; pass overrides to make it un-bakeable. */
export function standaloneEmu(
  label: string,
  is_default = false,
  overrides: Partial<EmulatorOption> = {},
): EmulatorOption {
  return { label, kind: "standalone", core_so: null, is_default, bakeable: true, reason: null, ...overrides };
}
