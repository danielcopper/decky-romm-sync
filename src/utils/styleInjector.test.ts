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

    // Steam repaints a focused destructive MenuItem red; without this the red
    // icon disappears into it. gpfocus is Steam's own class for the focused
    // element itself, so the descendant combinator resolves to that one row.
    expect(css).toMatch(/\.gpfocus \.romm-vanished-trash-row\s*\{[^}]*color:\s*#000/);
    // The flip must not be reachable through the base class alone — the
    // singleton binding's button keeps a dark focus background.
    expect(css).not.toMatch(/\.gpfocus \.romm-vanished-trash\s*[,{]/);
  });

  it("never reaches the trash through an ancestor-matching pseudo-class", () => {
    const css = injectedCss();

    // :focus-within matches every ancestor of the focused element up to <body>,
    // so `:focus-within .romm-vanished-trash-row` repaints EVERY vanished row's
    // trash while the menu is navigated — and at (0,2,0) it outranks the
    // (0,1,0) red base, so red would never paint on a menu row at all. `:focus`
    // on a container that wraps the rows has the same flaw in narrower form.
    expect(css).not.toMatch(/:focus-within[^,{]*\.romm-vanished-trash/);
    expect(css).not.toMatch(/:focus\s+\.romm-vanished-trash/);
  });

  it("removes the block again when the native play section is restored", () => {
    showNativePlaySection();

    expect(injectedCss()).not.toContain("romm-vanished-trash");
  });
});
