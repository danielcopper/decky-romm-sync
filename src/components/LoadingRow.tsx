import { FC } from "react";
import { PanelSectionRow, Spinner } from "@decky/ui";

/**
 * Fixed pixel size for QAM loading spinners. @decky/ui's Spinner is an SVG icon
 * that fills its container when unconstrained, so a bare `<Spinner />` inside a
 * PanelSectionRow renders oversized (#1414). Pinning width/height keeps every
 * section-level loading state the same small size.
 */
export const LOADING_SPINNER_SIZE = 24;

interface LoadingRowProps {
  /** Optional caption rendered beside the spinner (e.g. "Loading platforms…"). */
  label?: string;
}

/**
 * The canonical QAM loading state: a small, fixed-size spinner centered in a
 * PanelSectionRow. A section that shows a spinner while it loads renders this
 * instead of a bare `<Spinner />`, whose SVG otherwise fills the row.
 */
export const LoadingRow: FC<LoadingRowProps> = ({ label }) => (
  <PanelSectionRow>
    <div style={{ display: "flex", alignItems: "center", justifyContent: "center", gap: "8px", padding: "8px 0" }}>
      <Spinner width={LOADING_SPINNER_SIZE} height={LOADING_SPINNER_SIZE} />
      {label ? <span style={{ fontSize: "13px", opacity: 0.7 }}>{label}</span> : null}
    </div>
  </PanelSectionRow>
);
