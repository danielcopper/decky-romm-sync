/**
 * The list-and-detail layout of a wide QAM page: the list on the left, the
 * focused entry's detail on the right, each scrolling on its own inside the
 * frame's measured height.
 *
 * A region scrolls by moving focus, so what a reader must be able to reach in
 * either pane — a detail's table rows included — has to be focusable; plain
 * text scrolls along with the row it accompanies but cannot be scrolled to.
 *
 * Focus selects — moving through the list changes the detail at once, as Steam's
 * own settings do. The row's own control keeps A, so this component never
 * intercepts it; a row that carries a toggle still toggles on press. A control
 * that acts on the whole list goes in `listHeader` rather than in a row, so
 * reaching it reports no selection.
 *
 * Selection is controlled: the page owns `selectedId` and decides what a change
 * means for the rest of it.
 */

import type { FC, ReactNode } from "react";
import { Focusable } from "@decky/ui";
import { ScrollRegion } from "./ScrollRegion";

export interface ListDetailItem {
  id: string;
  render: (selected: boolean) => ReactNode;
}

export interface ListDetailProps {
  items: ListDetailItem[];
  selectedId: string | null;
  onSelect: (id: string) => void;
  renderDetail: (selectedId: string | null) => ReactNode;
  /**
   * Controls that act on the whole list — Enable all, Disable all — rendered
   * above the first row and scrolling with it. Outside every item, so focusing
   * one reports no selection: these belong to the list, not to a row of it.
   */
  listHeader?: ReactNode;
}

// The list takes about a third of the 806 px a wide tab panel offers.
const LIST_WIDTH = "264px";

export const ListDetail: FC<ListDetailProps> = ({ items, selectedId, onSelect, renderDetail, listHeader }) => (
  <Focusable flow-children="horizontal" style={{ display: "flex", gap: "12px", height: "100%", minHeight: 0 }}>
    <ScrollRegion style={{ flex: `0 0 ${LIST_WIDTH}`, width: LIST_WIDTH }}>
      <Focusable flow-children="vertical">
        {listHeader}
        {items.map((item) => (
          // React delivers onFocus through focusin, so this fires for focus
          // landing on whatever control the row itself renders — and again for
          // every move between controls inside the same row, or on the way back
          // from the detail pane. Only a real change is reported, so a page may
          // treat onSelect as an event and do work on it.
          <Focusable
            key={item.id}
            onFocus={() => {
              if (item.id !== selectedId) onSelect(item.id);
            }}
          >
            {item.render(item.id === selectedId)}
          </Focusable>
        ))}
      </Focusable>
    </ScrollRegion>
    <ScrollRegion style={{ flex: "1 1 auto", minWidth: 0 }}>
      <Focusable flow-children="vertical">{renderDetail(selectedId)}</Focusable>
    </ScrollRegion>
  </Focusable>
);
