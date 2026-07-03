import { describe, it, expect, vi } from "vitest";
import type { ReactElement } from "react";

// The builder is inspected as a React element tree, not rendered, so the
// @decky/ui menu components only need to be distinguishable element types. The
// global stub omits MenuSeparator; a local passthrough mock supplies all three.
vi.mock("@decky/ui", () => ({
  Menu: "Menu",
  MenuItem: "MenuItem",
  MenuSeparator: "MenuSeparator",
}));

import { buildEmulatorMenu, reasonCopy, type EmulatorMenuConfig } from "./emulatorMenu";
import { libretroEmu, standaloneEmu } from "../test-utils/coreFixtures";

// The builder returns a <Menu> element; walk its child MenuItem elements without
// rendering (the @decky/ui Menu/MenuItem are stubs). A MenuItem carries its label
// as a string child; MenuSeparators have no string child and are filtered out.
interface Item {
  text: string;
  disabled: boolean;
  onClick: (() => void) | undefined;
}
function items(menu: unknown): Item[] {
  const el = menu as ReactElement<{ children?: unknown }>;
  const kids = ([] as unknown[])
    .concat(el.props.children as unknown[])
    .flat(Infinity)
    .filter(Boolean);
  return kids
    .map((k) => k as ReactElement<{ children?: unknown; disabled?: boolean; onClick?: () => void }>)
    .filter((k) => typeof k.props.children === "string")
    .map((k) => ({ text: k.props.children as string, disabled: !!k.props.disabled, onClick: k.props.onClick }));
}

function baseConfig(overrides: Partial<EmulatorMenuConfig> = {}): EmulatorMenuConfig {
  return {
    emulators: [libretroEmu("mgba_libretro", "mGBA", true), libretroEmu("vbam_libretro", "VBA-M")],
    emulatorDataAvailable: true,
    activeLabel: null,
    platformCoreLabel: null,
    onPick: vi.fn(),
    ...overrides,
  };
}

describe("reasonCopy", () => {
  it("maps known reason slugs to distinct copy", () => {
    expect(reasonCopy("inject")).toBe("needs setup files (launch via ES-DE once)");
    expect(reasonCopy("not_installed")).toBe("emulator not installed");
    expect(reasonCopy("shortcut_script")).toBe("script/shortcut form");
  });

  it("falls back to a generic message for other slugs", () => {
    for (const r of ["no_rom_target", "quoting", "startdir", "unknown_placeholder", null]) {
      expect(reasonCopy(r)).toBe("not launchable from Steam");
    }
  });
});

describe("buildEmulatorMenu", () => {
  it("renders a single disabled notice when emulator data is unavailable", () => {
    const menu = buildEmulatorMenu(baseConfig({ emulators: [], emulatorDataAvailable: false }));
    const its = items(menu);
    expect(its).toHaveLength(1);
    expect(its[0]!.disabled).toBe(true);
    expect(its[0]!.text).toBe("Emulator list unavailable — RetroDECK installation not found");
  });

  it("marks the default emulator and dispatches its label on pick", () => {
    const onPick = vi.fn();
    const menu = buildEmulatorMenu(baseConfig({ onPick }));
    const its = items(menu);
    const mgba = its.find((i) => i.text.startsWith("mGBA"))!;
    expect(mgba.text).toContain("(default)");
    expect(mgba.disabled).toBe(false);
    mgba.onClick!();
    expect(onPick).toHaveBeenCalledWith("mGBA");
  });

  it("renders an un-bakeable emulator as disabled with its reason copy", () => {
    const menu = buildEmulatorMenu(
      baseConfig({
        emulators: [
          libretroEmu("mgba_libretro", "mGBA", true),
          standaloneEmu("RPCS3 Shortcut (Standalone)", false, { bakeable: false, reason: "shortcut_script" }),
          standaloneEmu("Vita3K (Standalone)", false, { bakeable: false, reason: "inject" }),
        ],
      }),
    );
    const its = items(menu);
    const shortcut = its.find((i) => i.text.startsWith("RPCS3 Shortcut"))!;
    expect(shortcut.disabled).toBe(true);
    expect(shortcut.text).toBe("RPCS3 Shortcut (Standalone) — script/shortcut form");
    expect(shortcut.onClick).toBeUndefined();
    const inject = its.find((i) => i.text.startsWith("Vita3K"))!;
    expect(inject.disabled).toBe(true);
    expect(inject.text).toBe("Vita3K (Standalone) — needs setup files (launch via ES-DE once)");
  });

  it("renders a not-installed standalone emulator as disabled with its reason copy", () => {
    const menu = buildEmulatorMenu(
      baseConfig({
        emulators: [
          libretroEmu("mgba_libretro", "mGBA", true),
          standaloneEmu("Ryubing (Standalone)", false, { bakeable: false, reason: "not_installed" }),
        ],
      }),
    );
    const ryubing = items(menu).find((i) => i.text.startsWith("Ryubing"))!;
    expect(ryubing.disabled).toBe(true);
    expect(ryubing.text).toBe("Ryubing (Standalone) — emulator not installed");
    expect(ryubing.onClick).toBeUndefined();
  });

  it("dispatches a bakeable standalone emulator's label on pick", () => {
    const onPick = vi.fn();
    const menu = buildEmulatorMenu(
      baseConfig({
        emulators: [standaloneEmu("RPCS3 Directory (Standalone)", true), standaloneEmu("RPCS3 ISO (Standalone)")],
        onPick,
      }),
    );
    const dir = items(menu).find((i) => i.text.startsWith("RPCS3 Directory"))!;
    expect(dir.disabled).toBe(false);
    dir.onClick!();
    expect(onPick).toHaveBeenCalledWith("RPCS3 Directory (Standalone)");
  });

  it("marks the checkmark on the default when no active override is set", () => {
    const menu = buildEmulatorMenu(baseConfig({ activeLabel: null }));
    const mgba = items(menu).find((i) => i.text.startsWith("mGBA"))!;
    expect(mgba.text).toContain("✓");
  });

  it("moves the checkmark onto the active override when one is set", () => {
    const menu = buildEmulatorMenu(baseConfig({ activeLabel: "VBA-M" }));
    const its = items(menu);
    expect(its.find((i) => i.text.startsWith("VBA-M"))!.text).toContain("✓");
    expect(its.find((i) => i.text.startsWith("mGBA"))!.text).not.toContain("✓");
  });

  it("marks the per-platform override with (system)", () => {
    const menu = buildEmulatorMenu(baseConfig({ platformCoreLabel: "VBA-M" }));
    expect(items(menu).find((i) => i.text.startsWith("VBA-M"))!.text).toContain("(system)");
  });

  it("adds the follow-system reset item (game-detail) and fires it", () => {
    const onFollowSystem = vi.fn();
    const menu = buildEmulatorMenu(baseConfig({ followSystem: { hasGameOverride: true, onFollowSystem } }));
    const follow = items(menu).find((i) => i.text.startsWith("Use System Override"))!;
    expect(follow.text).toBe("Use System Override (mGBA)");
    follow.onClick!();
    expect(onFollowSystem).toHaveBeenCalledOnce();
  });

  it("checkmarks the follow-system item when the game already follows the system", () => {
    const menu = buildEmulatorMenu(baseConfig({ followSystem: { hasGameOverride: false, onFollowSystem: vi.fn() } }));
    expect(items(menu).find((i) => i.text.startsWith("Use System Override"))!.text).toContain("✓");
  });

  it("omits the follow-system item on the System page (no followSystem)", () => {
    const menu = buildEmulatorMenu(baseConfig());
    expect(items(menu).some((i) => i.text.startsWith("Use System Override"))).toBe(false);
  });
});
