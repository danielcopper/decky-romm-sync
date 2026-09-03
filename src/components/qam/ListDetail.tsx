/**
 * The list-and-detail layout of a wide QAM page: the list on the left, the
 * focused entry's detail on the right, each scrolling on its own inside the
 * frame's measured height.
 *
 * Focus selects — moving through the list changes the detail at once, as Steam's
 * own settings do. The row's own control keeps A, so this component never
 * intercepts it; a row that carries a toggle still toggles on press.
 *
 * Selection is controlled: the page owns `selectedId` and decides what a change
 * means for the rest of it.
 */

import type { FC, ReactNode } from "react";
import { Focusable } from "@decky/ui";

export interface ListDetailItem {
  id: string;
  render: (selected: boolean) => ReactNode;
}

export interface ListDetailProps {
  items: ListDetailItem[];
  selectedId: string | null;
  onSelect: (id: string) => void;
  renderDetail: (selectedId: string | null) => ReactNode;
}

// The list takes about a third of the 806 px a wide tab panel offers.
const LIST_WIDTH = "264px";

const regionStyle = { height: "100%", overflow: "auto", minHeight: 0 } as const;

export const ListDetail: FC<ListDetailProps> = ({ items, selectedId, onSelect, renderDetail }) => (
  <Focusable flow-children="horizontal" style={{ display: "flex", gap: "12px", height: "100%", minHeight: 0 }}>
    <Focusable flow-children="vertical" style={{ ...regionStyle, flex: `0 0 ${LIST_WIDTH}`, width: LIST_WIDTH }}>
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
    <Focusable flow-children="vertical" style={{ ...regionStyle, flex: "1 1 auto", minWidth: 0 }}>
      {renderDetail(selectedId)}
    </Focusable>
  </Focusable>
);
