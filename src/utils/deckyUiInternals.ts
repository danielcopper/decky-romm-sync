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

import {
  appActionButtonClasses as _appActionButtonClasses,
  basicAppDetailsSectionStylerClasses as _basicAppDetailsSectionStylerClasses,
  appDetailsClasses as _appDetailsClasses,
  playSectionClasses as _playSectionClasses,
  findSP as _findSP,
} from "@decky/ui";

export const appActionButtonClasses: typeof _appActionButtonClasses | undefined = _appActionButtonClasses;
export const basicAppDetailsSectionStylerClasses: typeof _basicAppDetailsSectionStylerClasses | undefined =
  _basicAppDetailsSectionStylerClasses;
export const appDetailsClasses: typeof _appDetailsClasses | undefined = _appDetailsClasses;
export const playSectionClasses: typeof _playSectionClasses | undefined = _playSectionClasses;

export const findSP = (): Window | undefined => _findSP();
