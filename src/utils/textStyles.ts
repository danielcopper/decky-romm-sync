import type { CSSProperties } from "react";

/**
 * Shared style for dynamic text rendered in the narrow QAM panel (progress
 * captions, status messages, ROM / platform / collection names).
 *
 * The rule it encodes: text that can exceed the panel width must wrap to the
 * next line(s) on word boundaries and never clip, never truncate mid-word, and
 * never push the layout sideways. `overflowWrap: "break-word"` breaks an
 * over-long unbroken token (a long ROM name or filename) only when it cannot
 * fit a line on its own, so ordinary text still wraps at spaces. `minWidth: 0`
 * lets the text shrink and wrap inside a flex row instead of overflowing it
 * (flex items default to `min-width: auto`). `fontSize` matches the 12px used
 * by these secondary lines today.
 *
 * Steam's own `Field` wraps its label/description, so this is for the custom
 * spans we render ourselves (the bare-ProgressBar caption rows, #751). Callers
 * that need a hard line cap layer `-webkit-line-clamp` on top of this.
 */
export const wrapText: CSSProperties = {
  whiteSpace: "normal",
  overflowWrap: "break-word",
  minWidth: 0,
  fontSize: "12px",
};
