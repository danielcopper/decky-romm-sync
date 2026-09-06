/**
 * The layout of a wide page whose content is side-by-side scrolling regions:
 * one row of columns, each scrolling on its own inside the frame's measured
 * height.
 *
 * List and detail is one shape of it and `ListDetail` is that shape; the Sync
 * page's table beside its controls column is another. What is shared is the row
 * itself — a horizontal `Focusable` so the stick crosses between columns, the
 * gap between them, and a `ScrollRegion` per column so a long column does not
 * carry its neighbours up with it.
 *
 * Structure and vocabulary: `docs/architecture/qam-panel.md`.
 */

import type { FC, ReactNode } from "react";
import { Focusable } from "@decky/ui";
import { ScrollRegion } from "./ScrollRegion";

export interface Column {
  id: string;
  /** A fixed width, or nothing for the column that takes what is left. */
  width?: string;
  /**
   * What makes the page want this column REMOUNTED rather than re-rendered —
   * which is how a fresh region opens at its own top rather than at the offset
   * the previous content was left at. The key a region carries joins the
   * column's `id` to this, so a column that names none keeps one constant key
   * and is never remounted.
   *
   * A key rather than a ref that scrolls the region back: Steam's scroll panel
   * is reached through a webpack probe and nothing establishes that it forwards
   * one, while a key needs no handle on the element at all.
   */
  regionKey?: string;
  content: ReactNode;
}

export interface ColumnsProps {
  columns: Column[];
}

export const Columns: FC<ColumnsProps> = ({ columns }) => (
  <Focusable flow-children="horizontal" style={{ display: "flex", gap: "12px", height: "100%", minHeight: 0 }}>
    {columns.map((column) => (
      <ScrollRegion
        // The id and the region key are joined rather than one standing in
        // for the other: a page whose region keys are its item ids would
        // otherwise collide with a column named after one of them.
        key={`${column.id}:${column.regionKey ?? ""}`}
        // `minWidth: 0` on the flexible column for the reason the region's own
        // bounds reset its min-height: a flex item's floor is its content, so
        // without it one over-wide row widens the column past its share.
        style={
          column.width === undefined
            ? { flex: "1 1 auto", minWidth: 0 }
            : { flex: `0 0 ${column.width}`, width: column.width }
        }
      >
        {column.content}
      </ScrollRegion>
    ))}
  </Focusable>
);
