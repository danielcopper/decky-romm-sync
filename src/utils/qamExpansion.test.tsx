/**
 * qamExpansion tests — the two levers that widen the QAM panel, and the four
 * paths that have to clear them again.
 *
 * The levers themselves are Steam internals that happy-dom cannot show: no
 * FriendsUI store listens for the message, and no 300 px cap exists to lift. What
 * IS pinnable is everything the plugin owns — which message goes out, to which
 * target origin, which selector the injected rule is written against, and that
 * every exit path posts the hide message and drops the stylesheet.
 *
 * `quickAccessMenuClasses` is read once at module scope (the probe answers the
 * same value for the process), so each test loads a fresh copy of the module
 * through `loadQamExpansion` with the probe value it wants.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, act } from "@testing-library/react";
import { useRef, useSyncExternalStore, type FC } from "react";

// --- @decky/ui: only useQuickAccessVisible, backed by a store the tests flip.
// deckyUiInternals is mocked too, so nothing else in this graph reaches @decky/ui.
let qamVisible = true;
const visibilityListeners = new Set<() => void>();
const subscribeVisibility = (onChange: () => void) => {
  visibilityListeners.add(onChange);
  return () => {
    visibilityListeners.delete(onChange);
  };
};

vi.mock("@decky/ui", () => ({
  useQuickAccessVisible: () => useSyncExternalStore(subscribeVisibility, () => qamVisible),
}));

const ACTIVE_TAB_CLASS = "ActiveTab_hash";
const TAB_PANEL_CLASS = "TabGroupPanel_hash";
const PROBE_CLASSES = { ActiveTab: ACTIVE_TAB_CLASS, TabGroupPanel: TAB_PANEL_CLASS };

type QamExpansion = typeof import("./qamExpansion");

/**
 * A fresh copy of the module with `classes` as the webpack probe's answer.
 * `vi.doMock` rather than a hoisted `vi.mock`: the hoisted factory's result is
 * cached for the file, so both probe outcomes could not be exercised in one run.
 */
async function loadQamExpansion(classes: Record<string, string> | undefined): Promise<QamExpansion> {
  vi.resetModules();
  vi.doMock("./deckyUiInternals", () => ({ quickAccessMenuClasses: classes, Tabs: undefined }));
  return await import("./qamExpansion");
}

/**
 * The QAM shape the hook walks up through: the tab panel Decky's tab renders
 * into, inside the parent Steam marks with `ActiveTab`.
 */
function mountQamDom(): { host: HTMLElement; panelParent: HTMLElement } {
  const panelParent = document.createElement("div");
  panelParent.className = ACTIVE_TAB_CLASS;
  const panel = document.createElement("div");
  panel.id = "quickaccess_content_999";
  panel.className = TAB_PANEL_CLASS;
  const host = document.createElement("div");
  panel.appendChild(host);
  panelParent.appendChild(panel);
  document.body.appendChild(panelParent);
  return { host, panelParent };
}

function renderWidePage(mod: QamExpansion, host: HTMLElement) {
  const Page: FC = () => {
    const rootRef = useRef<HTMLDivElement>(null);
    mod.useWideQamPanel(rootRef);
    return <div ref={rootRef} className={mod.WIDE_ROOT_CLASS} />;
  };
  return render(<Page />, { container: host });
}

/** Every injected stylesheet carrying the wide-page rule. */
function wideStyles(markerClass: string): HTMLStyleElement[] {
  return [...document.head.querySelectorAll("style")].filter((el) => el.textContent.includes(markerClass));
}

describe("setQamExpanded", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("names Steam's own expand and hide messages", async () => {
    const { setQamExpanded } = await loadQamExpansion(PROBE_CLASSES);
    const post = vi.spyOn(window, "postMessage").mockImplementation(() => {});

    setQamExpanded(true);
    setQamExpanded(false);

    expect(post.mock.calls).toEqual([
      [{ message: "QamFriendsExpanded" }, window.origin],
      [{ message: "QamFriendsHidden" }, window.origin],
    ]);
  });

  it("posts to window.origin, the only target origin that does not throw", async () => {
    const { setQamExpanded } = await loadQamExpansion(PROBE_CLASSES);

    // A literal origin is what the spike shipped first: postMessage rejects a
    // target origin that is not the window's own, and one caller is onDismount,
    // where the throw abandons the rest of the plugin's teardown.
    expect(() => window.postMessage({ message: "QamFriendsExpanded" }, "https://steamloopback.host")).toThrow();
    expect(() => setQamExpanded(true)).not.toThrow();
    expect(() => setQamExpanded(false)).not.toThrow();
  });
});

describe("useWideQamPanel", () => {
  let post: ReturnType<typeof vi.spyOn<Window, "postMessage">>;

  const lastMessage = () => {
    const { calls } = post.mock;
    return (calls[calls.length - 1]?.[0] as { message: string } | undefined)?.message;
  };

  beforeEach(() => {
    qamVisible = true;
    visibilityListeners.clear();
    post = vi.spyOn(window, "postMessage").mockImplementation(() => {});
  });

  afterEach(() => {
    vi.restoreAllMocks();
    document.body.replaceChildren();
    document.head.replaceChildren();
  });

  it("expands and injects the rule while the page is mounted", async () => {
    const mod = await loadQamExpansion(PROBE_CLASSES);
    const { host } = mountQamDom();

    renderWidePage(mod, host);

    expect(lastMessage()).toBe("QamFriendsExpanded");
    expect(wideStyles(mod.WIDE_ROOT_CLASS)).toHaveLength(1);
  });

  it("never expands when the page mounts under an inactive Decky tab", async () => {
    const mod = await loadQamExpansion(PROBE_CLASSES);
    const { host, panelParent } = mountQamDom();
    panelParent.classList.remove(ACTIVE_TAB_CLASS);

    // The plugin's panel renders on while another QAM tab is active
    // (`alwaysRender`), so a wide page can mount here. Expanding and retracting
    // across two renders would flash Steam's own panel open.
    renderWidePage(mod, host);

    expect(post).not.toHaveBeenCalled();
    expect(wideStyles(mod.WIDE_ROOT_CLASS)).toHaveLength(0);
  });

  it("clears on unmount", async () => {
    const mod = await loadQamExpansion(PROBE_CLASSES);
    const { host } = mountQamDom();
    const { unmount } = renderWidePage(mod, host);

    unmount();

    expect(lastMessage()).toBe("QamFriendsHidden");
    expect(wideStyles(mod.WIDE_ROOT_CLASS)).toHaveLength(0);
  });

  it("clears when the Decky tab stops being the active QAM tab, and re-expands when it returns", async () => {
    const mod = await loadQamExpansion(PROBE_CLASSES);
    const { host, panelParent } = mountQamDom();
    renderWidePage(mod, host);

    // A QAM tab switch is a class change on the panel's parent, not an unmount:
    // the plugin's panel renders on (`alwaysRender`), so only the observer sees it.
    await act(async () => {
      panelParent.classList.remove(ACTIVE_TAB_CLASS);
    });

    expect(lastMessage()).toBe("QamFriendsHidden");
    expect(wideStyles(mod.WIDE_ROOT_CLASS)).toHaveLength(0);

    await act(async () => {
      panelParent.classList.add(ACTIVE_TAB_CLASS);
    });

    expect(lastMessage()).toBe("QamFriendsExpanded");
    expect(wideStyles(mod.WIDE_ROOT_CLASS)).toHaveLength(1);
  });

  it("clears when the QAM closes", async () => {
    const mod = await loadQamExpansion(PROBE_CLASSES);
    const { host } = mountQamDom();
    renderWidePage(mod, host);

    act(() => {
      qamVisible = false;
      for (const listener of visibilityListeners) listener();
    });

    expect(lastMessage()).toBe("QamFriendsHidden");
    expect(wideStyles(mod.WIDE_ROOT_CLASS)).toHaveLength(0);
  });

  it("clears from the plugin's dismount, where no React cleanup runs", async () => {
    const mod = await loadQamExpansion(PROBE_CLASSES);
    const { host } = mountQamDom();
    renderWidePage(mod, host);

    mod.collapseQamOnDismount();

    expect(lastMessage()).toBe("QamFriendsHidden");
    expect(wideStyles(mod.WIDE_ROOT_CLASS)).toHaveLength(0);
  });

  it("writes the rule against the probe's tab-panel class when it resolves", async () => {
    const mod = await loadQamExpansion(PROBE_CLASSES);
    const { host } = mountQamDom();
    renderWidePage(mod, host);

    const css = wideStyles(mod.WIDE_ROOT_CLASS)[0]?.textContent ?? "";
    expect(css).toContain(`.${TAB_PANEL_CLASS}:has(.${mod.WIDE_ROOT_CLASS})`);
    expect(css).toContain("max-width: none");
    expect(css).not.toContain("quickaccess_content_");
  });

  it("falls back to the panel's id prefix when the probe is undefined", async () => {
    const mod = await loadQamExpansion(undefined);
    const { host } = mountQamDom();
    renderWidePage(mod, host);

    const css = wideStyles(mod.WIDE_ROOT_CLASS)[0]?.textContent ?? "";
    expect(css).toContain(`[id^="quickaccess_content_"]:has(.${mod.WIDE_ROOT_CLASS})`);
    expect(css).not.toContain(TAB_PANEL_CLASS);
    // Nothing names the active-tab class either, so the tab question cannot be
    // asked — the page still expands rather than staying permanently narrow.
    expect(lastMessage()).toBe("QamFriendsExpanded");
  });
});
