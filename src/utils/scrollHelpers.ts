/**
 * Gamepad scroll helpers for injected game detail content.
 *
 * Steam's gamepad focus engine scrolls to focused elements automatically,
 * but its built-in handler doesn't center them reliably. These helpers
 * use a 50ms delayed scrollTo to override Steam's handler, ensuring
 * focused elements are centered (or scrolled to top) in the viewport.
 *
 * Only DialogButton works as a focusable element in this injection context —
 * Focusable wrappers around non-interactive content don't register with
 * Steam's gamepad engine when injected via routerHook.addPatch.
 */

/** Find the nearest ancestor that is actually scrollable (overflow:scroll|auto
 *  AND scrollHeight > clientHeight). */
export function findScrollParent(el: HTMLElement): HTMLElement | null {
  let parent: HTMLElement | null = el.parentElement;
  while (parent) {
    const ov = globalThis.getComputedStyle(parent).overflowY;
    if ((ov === "scroll" || ov === "auto") && parent.scrollHeight > parent.clientHeight) return parent;
    parent = parent.parentElement;
  }
  return null;
}

/** Find the outermost ancestor that is actually scrollable (overflow:scroll|auto
 *  AND scrollHeight > clientHeight). */
export function findOutermostScrollParent(el: HTMLElement): HTMLElement | null {
  let parent: HTMLElement | null = el.parentElement;
  let outermost: HTMLElement | null = null;
  while (parent) {
    const ov = globalThis.getComputedStyle(parent).overflowY;
    if ((ov === "scroll" || ov === "auto") && parent.scrollHeight > parent.clientHeight) outermost = parent;
    parent = parent.parentElement;
  }
  return outermost;
}

/** Minimal focus-event shape both DOM `FocusEvent` and React's `FocusEvent`
 *  satisfy — these helpers only ever read `currentTarget`. */
type FocusLike = { currentTarget: EventTarget | null };

/**
 * onFocus handler that scrolls the focused element to the center of the
 * scroll container. Use on DialogButton elements for gamepad navigation.
 */
export function scrollFocusedToCenter(e: FocusLike): void {
  const el = e.currentTarget as HTMLElement | null;
  setTimeout(() => {
    if (!el) return;
    const scrollParent = findScrollParent(el);
    if (scrollParent) {
      const elRect = el.getBoundingClientRect();
      const spRect = scrollParent.getBoundingClientRect();
      const targetScroll = scrollParent.scrollTop + (elRect.top - spRect.top) - spRect.height / 2 + elRect.height / 2;
      scrollParent.scrollTo({ top: targetScroll, behavior: "smooth" });
    }
  }, 50);
}

/**
 * onFocus handler that scrolls the NEAREST scroll container to its very top.
 *
 * For modal content, where the text the user has to read sits ABOVE the first
 * focusable element: Steam's focus engine only scrolls far enough to reveal the
 * focused element itself, so an intro above the first control stays permanently
 * off-screen on a controller. Put this on a wrapper around the topmost
 * selectable — React's onFocus is delivered via focusin, so it fires for a
 * focus landing on any descendant.
 *
 * Nearest, not outermost (`scrollToTop`): a modal's own scroll container is the
 * one that has to move, never the page scrolled behind it.
 */
export function scrollNearestToTop(e: FocusLike): void {
  const el = e.currentTarget as HTMLElement | null;
  setTimeout(() => {
    if (!el) return;
    findScrollParent(el)?.scrollTo({ top: 0, behavior: "smooth" });
  }, 50);
}

/**
 * onFocus handler that scrolls to the top of the scroll container.
 * Use on the Play button so navigating back up reveals the banner/hero.
 */
export function scrollToTop(e: FocusLike): void {
  const el = e.currentTarget as HTMLElement | null;
  setTimeout(() => {
    if (!el) return;
    // Use the outermost scroll parent so the banner/hero scrolls into view,
    // not just the nearest inner container.
    const scrollParent = findOutermostScrollParent(el);
    if (scrollParent) {
      scrollParent.scrollTo({ top: 0, behavior: "smooth" });
    }
  }, 50);
}

// Resolution-relative top clearance for scrollElementToTop, derived from the
// scroll parent's clientHeight so it scales with the viewport. The QAM's fixed
// "Tender" header sits over the scroll parent's top:0, and its height scales
// with resolution / UI scale (Deck 1280x800 vs Big Picture 1080p/1440p/4K), so
// a fixed px clears the header on only one display. `fraction * clientHeight`
// lands ~64px on the Deck QAM (on-device-tuned so the heading sits just clear of
// the header, not a tick too low) and scales up on larger displays; the cap
// keeps it from over-scrolling the field back off the top on very tall
// viewports. Tunable knobs.
const SCROLL_TOP_MARGIN_FRACTION = 0.1;
const SCROLL_TOP_MARGIN_CAP = 120;

/**
 * Scroll the outermost scroll parent so `el` sits near the TOP of the scroll
 * view, clear of the QAM's fixed header, instead of Steam's default
 * center-on-focus. Used to lift a field above the on-screen keyboard, which
 * covers the lower half of the screen — plain `scrollToTop` only reaches the
 * panel's top, which can leave a mid-panel field still under the keyboard.
 *
 * The top clearance is resolution-relative (see the constants above), derived
 * from the scroll parent's clientHeight so it holds across display sizes rather
 * than clearing the header at one resolution only.
 *
 * Takes the element directly (not a focus event) so a wrapper ref can be
 * scrolled even when focus lands on a child input. The 50ms delay overrides
 * Steam's own gamepad-focus scroll, matching the other helpers here.
 */
export function scrollElementToTop(el: HTMLElement | null): void {
  setTimeout(() => {
    if (!el) return;
    const scrollParent = findOutermostScrollParent(el);
    if (!scrollParent) return;
    // Resolution-aware clearance below the scroll parent's top edge.
    const topMargin = Math.min(SCROLL_TOP_MARGIN_CAP, scrollParent.clientHeight * SCROLL_TOP_MARGIN_FRACTION);
    // Cumulative offset of `el` within the scroll content: its current distance
    // from the scroll parent's top edge (getBoundingClientRect delta accounts
    // for all nesting) plus how far the parent is already scrolled. Setting
    // scrollTop to that (minus the clearance) lands the element's top just below
    // the fixed header.
    const elRect = el.getBoundingClientRect();
    const spRect = scrollParent.getBoundingClientRect();
    const targetScroll = scrollParent.scrollTop + (elRect.top - spRect.top) - topMargin;
    // Instant, not smooth: a smooth animation runs for several frames and races
    // the per-keystroke re-renders that follow the first-keystroke trigger, so
    // the vertical scrollbar toggling mid-animation jitters the field's width.
    // An instant jump settles the scroll in one frame, before typing continues.
    scrollParent.scrollTo({ top: Math.max(0, targetScroll), behavior: "auto" });
  }, 50);
}
