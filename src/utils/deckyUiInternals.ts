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
  ScrollPanelGroup as _ScrollPanelGroup,
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
 * `FC<{ children?: ReactNode }>`, which is narrower than it is: it spreads
 * everything it does not consume into Steam's `ScrollPanel`, which destructures
 * `className` and `style` and merges the style with its own scroll padding.
 */
export interface ScrollPanelProps {
  children?: ReactNode;
  className?: string;
  style?: CSSProperties;
}

/**
 * The scroll container that also scrolls on gamepad direction, which is the one
 * a region of unfocusable content needs: Steam's gamepad navigation scrolls by
 * moving focus, so a plain overflow box holding text nobody can focus never
 * scrolls at all.
 *
 * Despite the name it is not a wrapper around several scroll panels — it renders
 * one itself, with `onGamepadDirection` bound and its OK button focusing its
 * first visible child. Steam's plain `ScrollPanel` binds no direction and is not
 * re-exported here, because a region built on it would not scroll; it is what
 * the QAM's own tab panel and each tab's content are built from. Also a probe,
 * so also possibly `undefined`.
 */
export const ScrollPanelGroup: FC<ScrollPanelProps> | undefined = _ScrollPanelGroup;

export const findSP = (): Window | undefined => _findSP();
