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

import type { CSSProperties, FC, ReactNode } from "react";
import {
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
 * Steam's L1/R1 tabbed page, found through a `findModuleByExport` probe on the
 * shape of its render function — so it is `undefined` whenever that probe misses.
 * Upstream types it `any`, which hides both the absence and the props; a
 * component type states what the frame actually passes it.
 */
export const Tabs: FC<TabsProps> | undefined = _Tabs;

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
