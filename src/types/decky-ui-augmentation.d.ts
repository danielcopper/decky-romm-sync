/**
 * Module-augment @decky/ui types with props that exist at runtime on SteamUI
 * components but aren't declared in the upstream type definitions. Add new
 * augmentations here as we encounter `as any` casts that bypass missing
 * Decky-UI types.
 *
 * Note: @decky/ui's other event handlers (onClick, onPointerDown, etc.) take
 * DOM event types — keep the FocusEvent below consistent (global FocusEvent,
 * not React's synthetic FocusEvent).
 */

import type { CSSProperties } from "react";

export {};

declare module "@decky/ui" {
  interface DialogButtonProps {
    /** Fires when the focus ring lands on the button — used by our scroll-into-view helper. */
    onFocus?: (e: FocusEvent) => void;
    /** Native HTML title attribute (tooltip on hover). */
    title?: string;
  }

  interface FocusableProps {
    /**
     * Upstream declares `children: ReactNode` as required, but `createElement`
     * passes children positionally — so the props object never carries them
     * and the required marker fires a false positive. Widen to optional.
     */
    children?: import("react").ReactNode;
    /** Native HTML data-* attribute used as a marker for our patched panels. */
    "data-romm"?: string;
  }
}

declare global {
  /**
   * `React.CSSProperties` widened to accept CSS custom properties (`--*`).
   * React's stock `CSSProperties` rejects unknown keys, so styles that drive
   * animations via CSS vars need this looser shape.
   */
  type CSSPropertiesWithVars = CSSProperties & Record<`--${string}`, string | number>;
}
