import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import {
  findScrollParent,
  findOutermostScrollParent,
  scrollToTop,
  scrollFocusedToCenter,
  scrollElementToTop,
} from "./scrollHelpers";

/**
 * happy-dom does not compute layout. Set `scrollHeight` / `clientHeight` and
 * `getBoundingClientRect` explicitly so the helpers can decide scrollability
 * without a real browser.
 */
function makeElement(opts: {
  overflowY?: string;
  scrollHeight?: number;
  clientHeight?: number;
  rect?: { top: number; height: number };
}): HTMLDivElement {
  const el = document.createElement("div");
  if (opts.overflowY) el.style.overflowY = opts.overflowY;
  if (opts.scrollHeight !== undefined) {
    Object.defineProperty(el, "scrollHeight", { value: opts.scrollHeight, configurable: true });
  }
  if (opts.clientHeight !== undefined) {
    Object.defineProperty(el, "clientHeight", { value: opts.clientHeight, configurable: true });
  }
  if (opts.rect) {
    const r = opts.rect;
    el.getBoundingClientRect = () =>
      ({
        top: r.top,
        height: r.height,
        bottom: r.top + r.height,
        left: 0,
        right: 0,
        width: 0,
        x: 0,
        y: r.top,
        toJSON: () => ({}),
      }) as DOMRect;
  }
  return el;
}

/** Build a chain root → ... → leaf, append each to its parent, mount root in body. */
function chain(...elements: HTMLElement[]): HTMLElement {
  for (let i = 0; i < elements.length - 1; i++) {
    elements[i]!.appendChild(elements[i + 1]!); // i and i+1 both < elements.length
  }
  document.body.appendChild(elements[0]!); // caller always passes ≥1 element
  return elements[elements.length - 1]!;
}

afterEach(() => {
  while (document.body.firstChild) document.body.removeChild(document.body.firstChild);
});

describe("findScrollParent", () => {
  it("returns the first ancestor with overflow:auto AND scrollHeight > clientHeight", () => {
    const outer = makeElement({ overflowY: "auto", scrollHeight: 2000, clientHeight: 600 });
    const inner = makeElement({ overflowY: "auto", scrollHeight: 1000, clientHeight: 400 });
    const leaf = makeElement({});
    chain(outer, inner, leaf);

    expect(findScrollParent(leaf)).toBe(inner);
  });

  it("returns the first ancestor with overflow:scroll AND scrollHeight > clientHeight", () => {
    const outer = makeElement({ overflowY: "scroll", scrollHeight: 1500, clientHeight: 500 });
    const leaf = makeElement({});
    chain(outer, leaf);

    expect(findScrollParent(leaf)).toBe(outer);
  });

  it("skips an ancestor with overflow:auto but scrollHeight === clientHeight (regression-trigger case)", () => {
    // The Steam Beta wrapper case: overflow style is set but the wrapper has
    // no scrollable content of its own. Walk must skip it and find the real
    // scroll container further up.
    const real = makeElement({ overflowY: "auto", scrollHeight: 2000, clientHeight: 600 });
    const fakeWrapper = makeElement({ overflowY: "auto", scrollHeight: 500, clientHeight: 500 });
    const leaf = makeElement({});
    chain(real, fakeWrapper, leaf);

    expect(findScrollParent(leaf)).toBe(real);
  });

  it("skips an ancestor with overflow:hidden even when scrollHeight > clientHeight", () => {
    const hidden = makeElement({ overflowY: "hidden", scrollHeight: 2000, clientHeight: 600 });
    const leaf = makeElement({});
    chain(hidden, leaf);

    expect(findScrollParent(leaf)).toBeNull();
  });

  it("returns null when no scrollable ancestor exists", () => {
    const a = makeElement({ overflowY: "visible", scrollHeight: 600, clientHeight: 600 });
    const b = makeElement({ overflowY: "visible", scrollHeight: 600, clientHeight: 600 });
    const leaf = makeElement({});
    chain(a, b, leaf);

    expect(findScrollParent(leaf)).toBeNull();
  });
});

describe("findOutermostScrollParent", () => {
  it("returns the outermost scrollable ancestor when multiple exist", () => {
    const outer = makeElement({ overflowY: "auto", scrollHeight: 2000, clientHeight: 600 });
    const inner = makeElement({ overflowY: "auto", scrollHeight: 1000, clientHeight: 400 });
    const leaf = makeElement({});
    chain(outer, inner, leaf);

    expect(findOutermostScrollParent(leaf)).toBe(outer);
  });

  it("skips outer non-scrollable wrapper (issue #767 — inner scrollable, outer overflow:auto but no scrollable content)", () => {
    // The literal Steam Beta May 13 2026 regression: a new outer wrapper has
    // overflow:auto but no overflow content. Walking outward must not land on
    // it — the real page scroll container is the inner element.
    const fakeOuter = makeElement({ overflowY: "auto", scrollHeight: 600, clientHeight: 600 });
    const realInner = makeElement({ overflowY: "auto", scrollHeight: 2000, clientHeight: 600 });
    const leaf = makeElement({});
    chain(fakeOuter, realInner, leaf);

    expect(findOutermostScrollParent(leaf)).toBe(realInner);
  });

  it("returns null when no scrollable ancestor exists", () => {
    const a = makeElement({ overflowY: "visible", scrollHeight: 600, clientHeight: 600 });
    const leaf = makeElement({});
    chain(a, leaf);

    expect(findOutermostScrollParent(leaf)).toBeNull();
  });
});

describe("scrollToTop", () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  it("calls scrollTo({ top: 0, behavior: 'smooth' }) on the resolved scroll parent after the 50ms timer", () => {
    const outer = makeElement({ overflowY: "auto", scrollHeight: 2000, clientHeight: 600 });
    const leaf = makeElement({});
    chain(outer, leaf);
    const scrollTo = vi.fn();
    outer.scrollTo = scrollTo as unknown as typeof outer.scrollTo;

    scrollToTop({ currentTarget: leaf });
    expect(scrollTo).not.toHaveBeenCalled();
    vi.runAllTimers();

    expect(scrollTo).toHaveBeenCalledOnce();
    expect(scrollTo).toHaveBeenCalledWith({ top: 0, behavior: "smooth" });
  });

  it("is a no-op when no scroll parent is found", () => {
    const wrapper = makeElement({ overflowY: "visible", scrollHeight: 600, clientHeight: 600 });
    const leaf = makeElement({});
    chain(wrapper, leaf);
    const scrollTo = vi.fn();
    wrapper.scrollTo = scrollTo as unknown as typeof wrapper.scrollTo;

    scrollToTop({ currentTarget: leaf });
    vi.runAllTimers();

    expect(scrollTo).not.toHaveBeenCalled();
  });
});

describe("scrollFocusedToCenter", () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  it("centers the focused element in the resolved scroll parent", () => {
    // container at viewport top 0, 600px tall, scrolled 100px.
    // focused at viewport top 500, 50px tall.
    // expected targetScroll = 100 + (500 - 0) - (600 / 2) + (50 / 2) = 325
    const container = makeElement({
      overflowY: "auto",
      scrollHeight: 2000,
      clientHeight: 600,
      rect: { top: 0, height: 600 },
    });
    container.scrollTop = 100;
    const leaf = makeElement({ rect: { top: 500, height: 50 } });
    chain(container, leaf);
    const scrollTo = vi.fn();
    container.scrollTo = scrollTo as unknown as typeof container.scrollTo;

    scrollFocusedToCenter({ currentTarget: leaf });
    vi.runAllTimers();

    expect(scrollTo).toHaveBeenCalledOnce();
    expect(scrollTo).toHaveBeenCalledWith({ top: 325, behavior: "smooth" });
  });

  it("is a no-op when no scroll parent is found", () => {
    const wrapper = makeElement({ overflowY: "visible", scrollHeight: 600, clientHeight: 600 });
    const leaf = makeElement({ rect: { top: 0, height: 50 } });
    chain(wrapper, leaf);
    const scrollTo = vi.fn();
    wrapper.scrollTo = scrollTo as unknown as typeof wrapper.scrollTo;

    scrollFocusedToCenter({ currentTarget: leaf });
    vi.runAllTimers();

    expect(scrollTo).not.toHaveBeenCalled();
  });
});

describe("scrollElementToTop", () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  it("derives the top clearance from the OUTERMOST scroll parent's clientHeight (fraction path) after the 50ms timer", () => {
    // Outermost container: clientHeight 600, at viewport top 0, already scrolled
    // 100px. element at viewport top 400, 40px tall.
    // margin = min(120 cap, 600 * 0.10) = min(120, 60) = 60
    // expected targetScroll = 100 + (400 - 0) - 60 = 440
    const outer = makeElement({
      overflowY: "auto",
      scrollHeight: 2000,
      clientHeight: 600,
      rect: { top: 0, height: 600 },
    });
    outer.scrollTop = 100;
    const inner = makeElement({
      overflowY: "auto",
      scrollHeight: 1000,
      clientHeight: 400,
      rect: { top: 0, height: 400 },
    });
    const leaf = makeElement({ rect: { top: 400, height: 40 } });
    chain(outer, inner, leaf);
    const scrollTo = vi.fn();
    outer.scrollTo = scrollTo as unknown as typeof outer.scrollTo;

    scrollElementToTop(leaf);
    expect(scrollTo).not.toHaveBeenCalled();
    vi.runAllTimers();

    expect(scrollTo).toHaveBeenCalledOnce();
    // Instant (behavior: "auto") — the smooth animation raced typing re-renders
    // and jittered the field width, so the scroll settles in one frame.
    expect(scrollTo).toHaveBeenCalledWith({ top: 440, behavior: "auto" });
  });

  it("caps the top clearance on a tall viewport (cap path)", () => {
    // clientHeight 2000 → 2000 * 0.10 = 200, capped at 120.
    // expected targetScroll = 0 + (500 - 0) - 120 = 380
    const outer = makeElement({
      overflowY: "auto",
      scrollHeight: 5000,
      clientHeight: 2000,
      rect: { top: 0, height: 2000 },
    });
    outer.scrollTop = 0;
    const leaf = makeElement({ rect: { top: 500, height: 40 } });
    chain(outer, leaf);
    const scrollTo = vi.fn();
    outer.scrollTo = scrollTo as unknown as typeof outer.scrollTo;

    scrollElementToTop(leaf);
    vi.runAllTimers();

    expect(scrollTo).toHaveBeenCalledWith({ top: 380, behavior: "auto" });
  });

  it("clamps the target to 0 when the element sits above the margin (never scrolls negative)", () => {
    const outer = makeElement({
      overflowY: "auto",
      scrollHeight: 2000,
      clientHeight: 600,
      rect: { top: 0, height: 600 },
    });
    outer.scrollTop = 0;
    // element only 5px below the container top; margin = min(120, 600*0.10) = 60
    // → raw target 0 + 5 - 60 = -55 → clamp 0.
    const leaf = makeElement({ rect: { top: 5, height: 40 } });
    chain(outer, leaf);
    const scrollTo = vi.fn();
    outer.scrollTo = scrollTo as unknown as typeof outer.scrollTo;

    scrollElementToTop(leaf);
    vi.runAllTimers();

    expect(scrollTo).toHaveBeenCalledWith({ top: 0, behavior: "auto" });
  });

  it("is a no-op when no scroll parent is found", () => {
    const wrapper = makeElement({ overflowY: "visible", scrollHeight: 600, clientHeight: 600 });
    const leaf = makeElement({ rect: { top: 0, height: 40 } });
    chain(wrapper, leaf);
    const scrollTo = vi.fn();
    wrapper.scrollTo = scrollTo as unknown as typeof wrapper.scrollTo;

    scrollElementToTop(leaf);
    vi.runAllTimers();

    expect(scrollTo).not.toHaveBeenCalled();
  });

  it("is a no-op when the element is null", () => {
    // Guards the searchFieldRef.current === null case before first mount.
    expect(() => {
      scrollElementToTop(null);
      vi.runAllTimers();
    }).not.toThrow();
  });
});
