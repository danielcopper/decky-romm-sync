/**
 * The two levers that widen the plugin's Quick Access Menu panel from 348 px to
 * 854 px, and the paths that clear them again.
 *
 * Steam ships no API for either. Both are internals with no compatibility
 * promise, measured on the device rather than read from documentation; the
 * decision to use them, and what it rests on, is
 * `docs/adr/0029-wide-qam-pages-drive-steams-friends-expansion.md`.
 *
 * The flag the first lever sets is Steam's own and global, so whoever sets it
 * clears it: a wide page that leaks it leaves Steam's QAM expanded until the
 * Friends tab toggles it back. `useWideQamPanel` covers the three paths a
 * mounted page can observe; `collapseQamOnDismount` is the fourth.
 */

import { useEffect, type RefObject } from "react";
import { useQuickAccessVisible } from "@decky/ui";
import { quickAccessMenuClasses } from "./deckyUiInternals";

/** Marker class on a wide page's root, matched by the injected `:has()` rule. */
export const WIDE_ROOT_CLASS = "romm-wide-qam-root";

const WIDE_PANEL_STYLE_ID = "romm-wide-qam-styles";

// Decky registers a single QAM tab (`QuickAccessTab.Decky = 999`), so the
// plugin's tab panel is `#quickaccess_content_999` — the fallback for a
// `quickAccessMenuClasses` probe that came back undefined.
const TAB_PANEL_SELECTOR = quickAccessMenuClasses?.TabGroupPanel
  ? `.${quickAccessMenuClasses.TabGroupPanel}`
  : '[id^="quickaccess_content_"]';

// Every tab's content panel is capped at 300 px; only Steam's own Friends panel
// lifts it. The cap sits on the panel and on its first child, so both need the
// rule, and `:has()` scopes it to a panel holding a wide page of ours.
const WIDE_PANEL_CSS = `
${TAB_PANEL_SELECTOR}:has(.${WIDE_ROOT_CLASS}) { max-width: none; }
${TAB_PANEL_SELECTOR}:has(.${WIDE_ROOT_CLASS}) > * { max-width: none; }
`;

/**
 * Drive Steam's Friends-tab expansion. The FriendsUI store listens for `message`
 * events on the window plugin code runs in and flips one MobX observable, which
 * carries the `Expanded` class that un-shifts the QAM's placeholder.
 *
 * The target origin is always `window.origin`, never a literal: `postMessage`
 * throws on a mismatch, and one caller is `onDismount`, where a throw abandons
 * the rest of the plugin's teardown.
 */
export function setQamExpanded(expanded: boolean): void {
  window.postMessage({ message: expanded ? "QamFriendsExpanded" : "QamFriendsHidden" }, window.origin);
}

function removeWidePanelStyle(): void {
  document.getElementById(WIDE_PANEL_STYLE_ID)?.remove();
}

/**
 * Collapse the panel from the plugin's `onDismount`, where no React cleanup
 * runs any more. Safe to call when no wide page was ever mounted.
 */
export function collapseQamOnDismount(): void {
  setQamExpanded(false);
  removeWidePanelStyle();
}

/**
 * Hold the panel wide for as long as the page owning `rootRef` is mounted, the
 * Decky tab is the active QAM tab, and the QAM is open. Losing any of the three
 * posts the hide message and drops the stylesheet; regaining it re-expands.
 *
 * The tab question is answered from the DOM inside the effect rather than from
 * React state: the plugin's panel renders on when another QAM tab is active
 * (`alwaysRender`), so a page mounting there would expand on its first pass and
 * retract on the next — a visible flash of Steam's own panel.
 */
export function useWideQamPanel(rootRef: RefObject<HTMLElement | null>): void {
  const qamVisible = useQuickAccessVisible();

  useEffect(() => {
    if (!qamVisible) return;

    const activeTabClass = quickAccessMenuClasses?.ActiveTab;
    const panelParent = rootRef.current?.closest(TAB_PANEL_SELECTOR)?.parentElement;

    // True while the question cannot be asked — no panel around us, or a probe
    // that came back undefined so there is no class name to look for. The other
    // default would make every wide page permanently narrow; this one costs a
    // leaked expansion the QAM-close, unmount and dismount paths still clear.
    const deckyTabActive = () => !activeTabClass || !panelParent || panelParent.classList.contains(activeTabClass);

    let injected: HTMLStyleElement | null = null;

    const expand = () => {
      if (injected) return;
      setQamExpanded(true);
      injected = document.createElement("style");
      injected.id = WIDE_PANEL_STYLE_ID;
      injected.textContent = WIDE_PANEL_CSS;
      document.head.appendChild(injected);
    };

    const collapse = () => {
      if (!injected) return;
      setQamExpanded(false);
      injected.remove();
      injected = null;
    };

    const syncPanelWidth = () => (deckyTabActive() ? expand() : collapse());
    syncPanelWidth();

    // Switching QAM tabs changes the parent's class and unmounts nothing, so the
    // observer is the only thing that sees it.
    let observer: MutationObserver | null = null;
    if (activeTabClass && panelParent) {
      observer = new MutationObserver(syncPanelWidth);
      observer.observe(panelParent, { attributes: true, attributeFilter: ["class"] });
    }

    return () => {
      observer?.disconnect();
      collapse();
    };
  }, [qamVisible, rootRef]);
}
