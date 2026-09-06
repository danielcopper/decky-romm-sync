/**
 * The list-and-detail layout of a wide QAM page: the list on the left, the
 * focused entry's detail on the right, each scrolling on its own inside the
 * frame's measured height. It is `Columns` with those two columns.
 *
 * A region scrolls by moving focus, so what a reader must be able to reach in
 * either pane — a detail's table rows included — has to be focusable; plain
 * text scrolls along with the row it accompanies but cannot be scrolled to.
 *
 * Focus selects — moving through the list changes the detail at once, as Steam's
 * own settings do. The row's own control keeps A, so this component intercepts
 * it only where a page asks for `selectOnActivate`, for rows that carry no
 * control; a row that carries a toggle still toggles on press. A control that
 * acts on the whole list goes in `listHeader` rather than in a row, so reaching
 * it reports no selection.
 *
 * Selection is controlled: the page owns `selectedId` and decides what a change
 * means for the rest of it.
 */

import type { FC, ReactNode } from "react";
import { Focusable } from "@decky/ui";
import { Columns } from "./Columns";

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
  /**
   * Makes every row wrapper a focus stop by giving it an activate handler, for
   * a list whose rows carry no control of their own — a label and nothing else.
   * Without it such a row is a bare container that passes focus to children it
   * does not have, so the reader cannot reach it and cannot scroll past it.
   *
   * Off by default, because a row that DOES carry a control must leave A to it.
   */
  selectOnActivate?: boolean;
}

// The list takes about a third of the 806 px a wide tab panel offers.
const LIST_WIDTH = "264px";

export const ListDetail: FC<ListDetailProps> = ({
  items,
  selectedId,
  onSelect,
  renderDetail,
  listHeader,
  selectOnActivate,
}) => (
  <Columns
    columns={[
      {
        id: "list",
        width: LIST_WIDTH,
        content: (
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
                // Spread rather than a conditional value: under
                // `exactOptionalPropertyTypes` an explicit `undefined` is not
                // the same as an absent prop, and `FocusableProps` declares the
                // handler without it.
                {...(selectOnActivate ? { onActivate: () => onSelect(item.id) } : {})}
              >
                {item.render(item.id === selectedId)}
              </Focusable>
            ))}
          </Focusable>
        ),
      },
      {
        id: "detail",
        // Keyed on the selection so a new entry's detail opens at its own top.
        // Focus is in the list when this changes — that is what changed the
        // selection — so nothing focused is unmounted.
        regionKey: selectedId ?? "",
        content: <Focusable flow-children="vertical">{renderDetail(selectedId)}</Focusable>,
      },
    ]}
  />
);
