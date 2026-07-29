/**
 * styleInjector tests — the injected rules a component can only reference by
 * class name.
 *
 * A component that hands its colour to CSS has no way to assert the colour
 * itself: the stylesheet lands in Steam's SteamRoot document, which does not
 * exist under happy-dom. What CAN be pinned is that the rule the component
 * depends on is actually injected — without it the element renders with no
 * colour at all, which is a worse failure than the one the rule fixes.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";

const spWindow = { window: { document } } as unknown as Window;
vi.mock("./deckyUiInternals", () => ({ findSP: vi.fn(() => spWindow) }));

import { hideNativePlaySection, showNativePlaySection } from "./styleInjector";

/** Every rule block hideNativePlaySection injects, concatenated. */
function injectedCss(): string {
  return [...document.head.querySelectorAll("style")].map((el) => el.textContent).join("\n");
}

describe("styleInjector — vanished-version trash", () => {
  beforeEach(() => {
    showNativePlaySection();
    hideNativePlaySection("native-play");
  });

  afterEach(() => showNativePlaySection());

  it("colours the trash from CSS so the icon needs no inline colour", () => {
    expect(injectedCss()).toContain(".romm-vanished-trash {");
    expect(injectedCss()).toMatch(/\.romm-vanished-trash\s*\{[^}]*color:\s*#d94126/);
  });

  it("flips only the menu-row trash to black on gamepad focus", () => {
    const css = injectedCss();

    // Steam repaints a focused destructive MenuItem red; without these the red
    // icon disappears into it. gpfocus is Steam's own focus class; the :focus /
    // :focus-within siblings cover the row taking real DOM focus instead.
    expect(css).toContain(".gpfocus .romm-vanished-trash-row");
    expect(css).toContain(":focus .romm-vanished-trash-row");
    expect(css).toContain(":focus-within .romm-vanished-trash-row");
    expect(css).toMatch(/\.romm-vanished-trash-row[^{]*\{[^}]*color:\s*#000/);
    // The flip must not be reachable through the base class alone — the
    // singleton binding's button keeps a dark focus background.
    expect(css).not.toMatch(/\.gpfocus \.romm-vanished-trash\s*[,{]/);
  });

  it("removes the block again when the native play section is restored", () => {
    showNativePlaySection();

    expect(injectedCss()).not.toContain("romm-vanished-trash");
  });
});
