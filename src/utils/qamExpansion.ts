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
// plugin's panel is `#quickaccess_content_999`. Measured on the device: the id
// and the `TabGroupPanel` class are on that same element, so walking the DOM by
// id lands where the CSS below matches by class.
const PANEL_ID_SELECTOR = '[id^="quickaccess_content_"]';

const TAB_PANEL_SELECTOR = quickAccessMenuClasses?.TabGroupPanel
  ? `.${quickAccessMenuClasses.TabGroupPanel}`
  : PANEL_ID_SELECTOR;

// Steam caps every tab's content panel at 300 px and lifts it only for its own
// Friends panel (ADR-0029). The cap is on the panel element itself: with these
// rules up, the device measured `#quickaccess_content_999` at 806 px. The `> *`
// line covers a child carrying a cap of its own, which was never separately
// measured and costs one selector to keep. `:has()` scopes the lift to a panel
// holding a wide page of ours.
//
// The second rule is about focus rather than width, and rides along because it
// needs the same sheet in the same document. A DISABLED button is still a focus
// stop — the device pass walked one with the stick — but Steam's own disabled
// treatment (`opacity: .4` over `rgba(61,67,77,.35)`, `library.css`) leaves
// almost nothing for a focus fill to change, so the reader loses their place on
// a row of buttons where one is disabled. Steam answers this for its own
// variants with `background: #000` on `.DialogButton[disabled].gpfocus`, but
// those rules are scoped to hashed class names ours does not carry. The outline
// is Steam's other focus form, taken verbatim from the one it uses where a fill
// will not read (`outline: outset #fff 2px`, `chunk~2dcc5aaf7.css`), and it is
// scoped to a wide page of ours.
const WIDE_PANEL_CSS = `
${TAB_PANEL_SELECTOR}:has(.${WIDE_ROOT_CLASS}) { max-width: none; }
${TAB_PANEL_SELECTOR}:has(.${WIDE_ROOT_CLASS}) > * { max-width: none; }
.${WIDE_ROOT_CLASS} button.DialogButton[disabled].gpfocus,
.${WIDE_ROOT_CLASS} button.DialogButton.Disabled.gpfocus { outline: outset #fff 2px; }
`;

// The stylesheet a wide page has up, held by reference rather than looked up by
// id: it lives in the page's own document, and plugin code runs in a different
// one, so the ambient `document` cannot reach it.
//
// One reference for the module, which holds only while at most one wide page is
// mounted — the panel's router replaces the mounted page, it never stacks two.
// Break that and the reference stops belonging to anyone: the second page's
// mount is a no-op on an expansion it did not take, and its unmount collapses
// the panel under the first — stylesheet dropped, hide message posted, and the
// page still on screen has no path back to wide short of a QAM tab switch.
let injectedStyle: HTMLStyleElement | null = null;

/**
 * Drive Steam's Friends-tab expansion. The FriendsUI store listens for `message`
 * events on the window plugin code runs in and flips one MobX observable, which
 * carries the `Expanded` class that un-shifts the QAM's placeholder.
 *
 * The target origin is `window.origin`: it addresses the message to the one
 * window meant to receive it, and always matches, so the message is always
 * delivered. Neither alternative is wanted — a literal that ever stops matching
 * is checked at delivery and discarded in silence, leaving a panel that simply
 * never widens, and `"*"` is delivered to any document in the window.
 */
export function setQamExpanded(expanded: boolean): void {
  window.postMessage({ message: expanded ? "QamFriendsExpanded" : "QamFriendsHidden" }, window.origin);
}

/**
 * Give the panel's width back, if this plugin ever took it. A no-op otherwise:
 * the flag is Steam's own and global, so posting the hide message unasked would
 * retract a Friends panel the user opened.
 */
function collapseWidePanel(): void {
  const style = injectedStyle;
  if (!style) return;
  injectedStyle = null;
  style.remove();
  setQamExpanded(false);
}

function expandWidePanel(root: HTMLElement): void {
  if (injectedStyle) return;
  setQamExpanded(true);
  const doc = root.ownerDocument;
  const style = doc.createElement("style");
  style.id = WIDE_PANEL_STYLE_ID;
  style.textContent = WIDE_PANEL_CSS;
  doc.head.appendChild(style);
  injectedStyle = style;
}

/**
 * Collapse the panel from the plugin's `onDismount`, where no React cleanup runs
 * any more.
 */
export function collapseQamOnDismount(): void {
  collapseWidePanel();
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
    const root = rootRef.current;
    if (!qamVisible || !root) return;

    const activeTabClass = quickAccessMenuClasses?.ActiveTab;
    const panelParent = root.closest(PANEL_ID_SELECTOR)?.parentElement;

    // True while the question cannot be asked — no panel around us, or a probe
    // that came back undefined so there is no class name to look for. The other
    // default would make every wide page permanently narrow; this one costs a
    // leaked expansion the QAM-close, unmount and dismount paths still clear.
    const deckyTabActive = () => !activeTabClass || !panelParent || panelParent.classList.contains(activeTabClass);

    const syncPanelWidth = () => (deckyTabActive() ? expandWidePanel(root) : collapseWidePanel());
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
      collapseWidePanel();
    };
  }, [qamVisible, rootRef]);
}
