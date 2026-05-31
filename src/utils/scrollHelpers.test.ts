import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { findScrollParent, findOutermostScrollParent, scrollToTop, scrollFocusedToCenter } from "./scrollHelpers";

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
