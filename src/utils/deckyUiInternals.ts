/**
 * Honest-typed re-exports of @decky/ui internal lookups whose runtime presence
 * isn't guaranteed.
 *
 * @decky/ui populates its class-map consts and `findSP` via `findClassModule` /
 * webpack module probes that can return `undefined` at runtime — its own code
 * even writes `findSP() || window`. Upstream still types them as always-present
 * (`declare const x: T`, `findSP(): Window`), so direct consumers get a lying
 * non-null type and their defensive `?.` guards read as dead code. TS cannot
 * re-type a `const`/function via `declare module` augmentation, so this thin
 * runtime re-export module re-declares each as `T | undefined`, making the
 * guards legitimate.
 *
 * Any future @decky/ui value sourced from a findClassModule-style probe belongs
 * here, typed honestly.
 */

import type { CSSProperties, FC, FocusEventHandler, ReactNode } from "react";
import {
  findModule,
  appActionButtonClasses as _appActionButtonClasses,
  basicAppDetailsSectionStylerClasses as _basicAppDetailsSectionStylerClasses,
  appDetailsClasses as _appDetailsClasses,
  playSectionClasses as _playSectionClasses,
  quickAccessMenuClasses as _quickAccessMenuClasses,
  ScrollPanel as _ScrollPanel,
  Tabs as _Tabs,
  findSP as _findSP,
  type TabsProps,
} from "@decky/ui";

export const appActionButtonClasses: typeof _appActionButtonClasses | undefined = _appActionButtonClasses;
export const basicAppDetailsSectionStylerClasses: typeof _basicAppDetailsSectionStylerClasses | undefined =
  _basicAppDetailsSectionStylerClasses;
export const appDetailsClasses: typeof _appDetailsClasses | undefined = _appDetailsClasses;
export const playSectionClasses: typeof _playSectionClasses | undefined = _playSectionClasses;
export const quickAccessMenuClasses: typeof _quickAccessMenuClasses | undefined = _quickAccessMenuClasses;

/**
 * What the tabbed page accepts beyond the four props `@decky/ui` declares.
 *
 * `cancelSkipTabHeader` is Steam's own — its tabbed page renders the content
 * pane as `onCancelButton: !cancelSkipTabHeader && <focus the tab row>`
 * (`chunk~2dcc5aaf7.js`, the `TabContents` `Focusable`), so passing it true
 * leaves no cancel handler there and B travels on to whatever ancestor binds
 * it. Steam passes it itself in the controller-configurator dialogs. Upstream's
 * `TabsProps` predates the prop; the component is typed `any` there, so nothing
 * would have caught the name being wrong either.
 */
export interface WideTabsProps extends TabsProps {
  cancelSkipTabHeader?: boolean;
}

/**
 * Steam's L1/R1 tabbed page, found through a `findModuleByExport` probe on the
 * shape of its render function — so it is `undefined` whenever that probe misses.
 * Upstream types it `any`, which hides both the absence and the props; a
 * component type states what the frame actually passes it.
 */
export const Tabs: FC<WideTabsProps> | undefined = _Tabs;

/**
 * What the scroll panel accepts beyond its children. Upstream types it
 * `FC<{ children?: ReactNode }>`, which is narrower than it is: it destructures
 * `className` and `style`, merges the style with its own scroll padding, and
 * spreads the rest into the same base panel `Focusable` renders.
 */
export interface ScrollPanelProps {
  children?: ReactNode;
  className?: string;
  style?: CSSProperties;
  /** Reaches the rendered element: the panel spreads what it does not
   *  destructure into the base panel `Focusable` renders, which is where the
   *  attribute lands. React delivers it through `focusin`, so it fires for a
   *  focus landing on any descendant. */
  onFocus?: FocusEventHandler<HTMLDivElement>;
}

/**
 * Steam's plain scroll container: an `overflow-y: auto` box that takes no focus
 * of its own, so the rows inside it take focus directly and Steam's navigation
 * scrolls the focused row into view. It is what the QAM's own tab panel is built
 * from — the element carrying `#quickaccess_content_999` IS one of these — and
 * what Steam's tabbed page wraps each tab's content in.
 *
 * Its sibling `ScrollPanelGroup` binds gamepad direction to scrolling, which is
 * what a region of content nobody can focus would need; it was tried on the
 * device and rejected, because it is focusable and its OK button focuses its
 * first visible child, making every region a stop the reader has to enter with A
 * before reaching a row. Reachability is bought the other way instead: every row
 * a reader must reach is a focusable row (`docs/architecture/qam-panel.md`).
 *
 * Reached through a `findModuleByExport` probe on its render function, so it is
 * `undefined` whenever that probe misses.
 */
export const ScrollPanel: FC<ScrollPanelProps> | undefined = _ScrollPanel;

export const findSP = (): Window | undefined => _findSP();

/**
 * What Steam's controller-glyph image takes. `button` is Steam's OWN button
 * enum and not `@decky/ui`'s `GamepadButton` — the two disagree on every value
 * ({@link GLYPH_BUTTON_B}).
 */
export interface ControllerGlyphProps {
  button: number;
  /** The monochrome silhouette, which is what Steam uses inside running text. */
  bKnockout?: boolean;
  className?: string;
  style?: CSSProperties;
}

/**
 * The value Steam's glyph component wants for **B**.
 *
 * It indexes Steam's own action-button enum — `A=0, B=1, X=2, Y=3, Left=4 … `
 * (`chunk~2dcc5aaf7.js`, module 43014) — which is a different enum from
 * `@decky/ui`'s `GamepadButton` (`INVALID=0, OK=1, CANCEL=2 …`), where 1 is the
 * A button. Passing the wrong one draws the wrong glyph rather than failing.
 */
export const GLYPH_BUTTON_B = 1;

const isMemoComponent = (value: unknown): boolean =>
  typeof value === "object" && value !== null && (value as { $$typeof?: symbol }).$$typeof === Symbol.for("react.memo");

/**
 * Steam's controller-glyph image: one button drawn the way the controller in
 * the user's hands draws it — B on a Deck or an Xbox pad, ○ on a PlayStation
 * one, and the swapped face button under a Nintendo layout. It reads the active
 * controller itself, which is why neither a lookalike SVG nor a typed letter is
 * an alternative: both would be wrong for someone.
 *
 * `@decky/ui` does not re-export it, so it is reached by a module probe. Its
 * module exports exactly two values — this glyph and the footer's glyph-plus-
 * label pair — and that shape is unique: across every `.js` file in this
 * install's `steamui`, exactly one module declares two exports under those two
 * names and nothing else. Both are mobx observers, which is a `React.memo`
 * object at runtime and the second half of the filter.
 *
 * `undefined` the day Steam renames or re-splits that module, so every caller
 * keeps a text fallback rather than a hole where the glyph was.
 */
export const ControllerGlyph: FC<ControllerGlyphProps> | undefined = findModule((m: unknown) => {
  try {
    if (typeof m !== "object" || m === null) return false;
    const exports = m as Record<string, unknown>;
    const names = Object.keys(exports);
    return names.length === 2 && isMemoComponent(exports.W) && isMemoComponent(exports.X);
  } catch {
    return false;
  }
})?.W;
