/**
 * Shared builder for the emulator-picker context menu (#1210).
 *
 * Both pickers — the game-detail core menu (`RomMPlaySection`) and the System
 * page per-platform menu (`SystemPage`) — render the same list: every ES-DE
 * `<command>` classified for bakeability, with the bakeable ones clickable and
 * the un-bakeable ones shown disabled with a short reason. The frontend owns the
 * reason copy; the backend only ships the reason slug. Keys are the emulator
 * LABEL (not `core_so`), since a standalone emulator has no core.
 */

import type { ReactNode } from "react";
import { Menu, MenuItem, MenuSeparator } from "@decky/ui";
import type { EmulatorOption } from "../types";

/** Map a backend un-bakeable reason slug to short menu copy. */
export function reasonCopy(reason: string | null): string {
  switch (reason) {
    case "inject":
      return "needs setup files (launch via ES-DE once)";
    case "not_installed":
      return "emulator not installed";
    case "shortcut_script":
      return "script/shortcut form";
    default:
      // no_rom_target / quoting / startdir / unknown_placeholder / null
      return "not launchable from Steam";
  }
}

export interface EmulatorMenuConfig {
  emulators: EmulatorOption[];
  /** False when es_systems.xml can't be read — the menu says so instead of showing an empty list. */
  emulatorDataAvailable: boolean;
  /** The active emulator's label — marked with a checkmark. */
  activeLabel: string | null;
  /** The per-platform override label — marked "(system)". Null on the System page (redundant there). */
  platformCoreLabel: string | null;
  /**
   * Game-detail only: the "Use System Override" reset item that CLEARS the
   * per-game pin. Omit on the System page, where picking the default entry is
   * itself the clear-to-empty-label action.
   */
  followSystem?: { hasGameOverride: boolean; onFollowSystem: () => void };
  /** Called with the picked emulator LABEL when a bakeable entry is chosen. */
  onPick: (label: string) => void;
}

/**
 * The entry text for a bakeable emulator: its label plus the markers saying what
 * it is. "(default)" is the es_systems default, "(system)" the per-platform
 * override, ✓ the one actually in effect — one entry can carry all three, which
 * is why they are suffixes rather than a single state.
 */
function emulatorEntryLabel(e: EmulatorOption, isActive: boolean, isPlatformCore: boolean): string {
  const defaultMark = e.is_default ? " (default)" : "";
  const systemMark = isPlatformCore ? " (system)" : "";
  const activeMark = isActive ? " ✓" : "";
  return `${e.label}${defaultMark}${systemMark}${activeMark}`;
}

/** Build the `<Menu>` element for `showContextMenu`. */
export function buildEmulatorMenu(config: EmulatorMenuConfig): ReactNode {
  const { emulators, emulatorDataAvailable, activeLabel, platformCoreLabel, followSystem, onPick } = config;

  if (!emulatorDataAvailable) {
    return (
      <Menu label="Emulator">
        <MenuItem key="unavailable" disabled={true}>
          Emulator list unavailable — RetroDECK installation not found
        </MenuItem>
      </Menu>
    );
  }

  const defaultLabel = emulators.find((e) => e.is_default)?.label ?? null;
  // The active emulator is "the default" when nothing overrides it (no active
  // label, or it equals the default) — the checkmark then sits on the default.
  const activeIsDefault = !activeLabel || activeLabel === defaultLabel;

  const children: ReactNode[] = [
    <MenuItem key="compat" disabled={true}>
      Switching cores may affect save compatibility
    </MenuItem>,
    <MenuSeparator key="compat-sep" />,
  ];

  if (followSystem) {
    // The core the game falls back to with no per-game pin: the per-platform
    // override when set, else the es_systems default. ✓ sits here when the game
    // already follows the system (no per-game pin). "System Override" stays
    // distinct from the "(default)" marker so the menu never shows two defaults.
    const fallbackLabel = platformCoreLabel ?? defaultLabel ?? null;
    const fallbackSuffix = fallbackLabel ? ` (${fallbackLabel})` : "";
    const followsSystemMark = followSystem.hasGameOverride ? "" : " ✓";
    children.push(
      <MenuItem key="follow-system" onClick={followSystem.onFollowSystem}>
        {`Use System Override${fallbackSuffix}${followsSystemMark}`}
      </MenuItem>,
      <MenuSeparator key="follow-sep" />,
    );
  }

  for (const e of emulators) {
    const key = `emu-${e.label}`;
    if (!e.bakeable) {
      children.push(<MenuItem key={key} disabled={true}>{`${e.label} — ${reasonCopy(e.reason)}`}</MenuItem>);
      continue;
    }
    // The active marker sits on the ACTIVE emulator: the default-marked entry
    // when nothing overrides, otherwise the one whose label matches the active.
    const isActive = activeIsDefault ? e.is_default : activeLabel === e.label;
    const isPlatformCore = platformCoreLabel !== null && e.label === platformCoreLabel;
    children.push(
      <MenuItem key={key} onClick={() => onPick(e.label)}>
        {emulatorEntryLabel(e, isActive, isPlatformCore)}
      </MenuItem>,
    );
  }

  return <Menu label="Emulator Core">{children}</Menu>;
}
