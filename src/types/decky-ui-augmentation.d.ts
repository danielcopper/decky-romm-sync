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

export {};

declare module "@decky/ui" {
  interface DialogButtonProps {
    /** Fires when the focus ring lands on the button — used by our scroll-into-view helper. */
    onFocus?: (e: FocusEvent) => void;
  }
}
