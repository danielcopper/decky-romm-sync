/**
 * Section chrome for the RomM game detail panel and its tabs.
 *
 * A section is a DialogButton styled not to look like a button: DialogButton is
 * natively focusable by Steam's gamepad engine, unlike Focusable wrappers around
 * non-interactive content, which don't register in this injection context.
 * Steam's outer scroll container then auto-scrolls to the focused section.
 *
 * Uses createElement (no JSX) to match the panel. CSS classes prefixed with
 * `romm-panel-` are injected separately by styleInjector.
 */

import { createElement } from "react";
import { DialogButton } from "@decky/ui";
import { scrollFocusedToCenter } from "../utils/scrollHelpers";

/** A labeled info row: LABEL on the left, value on the right. */
export function infoRow(key: string, label: string, value: string): ReturnType<typeof createElement> {
  return createElement(
    "div",
    { key, className: "romm-panel-info-row" },
    createElement("span", { className: "romm-panel-label" }, label),
    createElement("span", { className: "romm-panel-value" }, value),
  );
}

/** A section with an optional title and children. */
export function section(
  key: string,
  title: string | null,
  ...children: (ReturnType<typeof createElement> | null)[]
): ReturnType<typeof createElement> {
  return createElement(
    DialogButton,
    {
      key,
      className: "romm-panel-section",
      style: {
        background: "transparent",
        border: "none",
        padding: "12px 0",
        textAlign: "left" as const,
        width: "100%",
        cursor: "default",
        display: "block",
      },
      noFocusRing: false,
      onFocus: scrollFocusedToCenter,
    },
    title ? createElement("div", { className: "romm-panel-section-title" }, title) : null,
    ...children.filter(Boolean),
  );
}
