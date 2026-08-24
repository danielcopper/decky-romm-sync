/**
 * Section chrome for the RomM game detail panel and its tabs.
 *
 * A section is a DialogButton styled not to look like a button: DialogButton is
 * natively focusable by Steam's gamepad engine, unlike Focusable wrappers around
 * non-interactive content, which don't register in this injection context.
 * Steam's outer scroll container then auto-scrolls to the focused section.
 *
 * CSS classes prefixed with `romm-panel-` are injected separately by
 * styleInjector.
 */

import type { ReactElement } from "react";
import { DialogButton } from "@decky/ui";
import { scrollFocusedToCenter } from "../utils/scrollHelpers";

/** A labeled info row: LABEL on the left, value on the right. */
export function infoRow(key: string, label: string, value: string): ReactElement {
  return (
    <div key={key} className="romm-panel-info-row">
      <span className="romm-panel-label">{label}</span>
      <span className="romm-panel-value">{value}</span>
    </div>
  );
}

/** A section with an optional title and children. */
export function section(key: string, title: string | null, ...children: (ReactElement | null)[]): ReactElement {
  return (
    <DialogButton
      key={key}
      className="romm-panel-section"
      style={{
        background: "transparent",
        border: "none",
        padding: "12px 0",
        textAlign: "left" as const,
        width: "100%",
        cursor: "default",
        display: "block",
      }}
      noFocusRing={false}
      onFocus={scrollFocusedToCenter}
    >
      {title ? <div className="romm-panel-section-title">{title}</div> : null}
      {children.filter(Boolean)}
    </DialogButton>
  );
}
